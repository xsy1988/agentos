"""抽取 + 片段级二次精排。

为什么抽取层还要再排一次：引擎摘要常常恰好错过真正回答问题的那一段，而整页正文又会用
导航、页脚、推荐位稀释关键信息。做法是「正文 → 切片 → 词覆盖预筛 → query × 候选片段
再排一次 → 每篇只留分数最高且过阈值的 2 段」，把最省 token 的证据交到模型手上。

预筛不是可有可无的优化：CPU 上 cross-encoder 每一对都是几百毫秒，一页几十块全送上去
就是十几秒。先用零成本的词覆盖分把候选压到个位数，是与主漏斗同构的第二级漏斗。

抽取分层（`EXTRACTOR=auto`）：先走进程内 trafilatura + httpx 快路（无浏览器、亚秒级），
当正文不足 `extract_min_chars` 或被 403/JS 拦截时升级到 Crawl4AI `/md`（Chromium 渲染）。
把浏览器只花在真正需要的页面上——延迟与内存都可控。

纪律：抽取失败/超时**只降级不失败**，退回引擎摘要并标 `extract=snippets_only|partial`。
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from websearch.cache import AsyncStore, url_key
from websearch.chunk import chunk_text
from websearch.config import Settings
from websearch.normalize import normalize_url, site_label
from websearch.rerank import rerank_passages
from websearch.types import Doc

# 常见反爬状态码：直接升级 Crawl4AI，不在快路上重试
_BLOCKED_STATUS = (401, 403, 405, 429, 503)
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_WS_RE = re.compile(r"\s+")


@dataclass
class ExtractOutcome:
    attempted: int = 0
    extracted: int = 0
    status: str = "snippets_only"  # ok|partial|snippets_only|disabled
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)

    def note(self, source: str) -> None:
        self.sources[source] = self.sources.get(source, 0) + 1


def _squash(text: str) -> str:
    """片段内不留多余空白（输出预算按字符算，空白是纯浪费）。"""
    return _WS_RE.sub(" ", text or "").strip()


# ---------------------------------------------------------------- 快路 trafilatura
def _trafilatura_run(html: str, url: str) -> str:
    """同步 CPU 活，调用方用 to_thread 包起来。"""
    try:
        import trafilatura  # noqa: PLC0415 —— 可选依赖，缺失时走升级路径
    except ImportError:
        return ""
    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 —— 抽取器内部异常一律当「抽不到」
        return ""
    return text or ""


async def _fast_extract(client: httpx.AsyncClient, url: str, timeout: float) -> tuple[str, str]:
    """返回 (正文, 状态)。状态 ∈ ok|blocked|empty|error。"""
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as e:
        return "", f"error:{type(e).__name__}"
    if resp.status_code in _BLOCKED_STATUS:
        return "", f"blocked:{resp.status_code}"
    if resp.status_code >= 400:
        return "", f"error:HTTP{resp.status_code}"
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return "", "error:not_html"
    html = await asyncio.to_thread(lambda: resp.text)
    text = await asyncio.to_thread(_trafilatura_run, html, url)
    if not text.strip():
        return "", "empty"
    return text.strip(), "ok"


# ---------------------------------------------------------------- 升级路 Crawl4AI
async def _crawl4ai_extract(
    client: httpx.AsyncClient, url: str, timeout: float, cfg: Settings, query: str
) -> tuple[str, str]:
    """Crawl4AI `/md`（Chromium 渲染 + fit 过滤）。返回 (正文, 状态)。"""
    headers = {"Authorization": f"Bearer {cfg.crawler_token}"} if cfg.crawler_token else {}
    try:
        resp = await client.post(
            cfg.crawler_url.rstrip("/") + "/md",
            json={"url": url, "f": "fit", "q": query or None},
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        return "", f"error:{type(e).__name__}"
    if resp.status_code != 200:
        return "", f"error:HTTP{resp.status_code}"
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        return "", "error:bad_json"
    if not data.get("success", False):
        return "", f"error:{str(data.get('error') or 'crawl_failed')[:60]}"
    text = str(data.get("markdown") or "").strip()
    return (text, "ok") if text else ("", "empty")


# ---------------------------------------------------------------- 单页正文（带缓存）
async def fetch_text(
    client: httpx.AsyncClient,
    doc: Doc,
    query: str,
    cfg: Settings,
    store: AsyncStore | None = None,
    *,
    fresh: bool = False,
    timeout: float | None = None,
) -> tuple[str, str]:
    """取单页正文，返回 (text, source)。source ∈ cache|trafilatura|crawl4ai|none。"""
    mode = (cfg.extractor or "auto").lower()
    page_timeout = timeout if timeout and timeout > 0 else cfg.extract_page_timeout
    if mode == "off":
        return "", "none"
    if store is not None:
        cached = await store.get_url(url_key(doc.norm_url))
        if cached and str(cached.get("text") or "").strip():
            return str(cached["text"]), "cache"

    url = doc.url or doc.norm_url
    text, source = "", "none"
    if mode in ("auto", "trafilatura"):
        text, status = await _fast_extract(client, url, page_timeout)
        source = "trafilatura" if text else "none"
        need_upgrade = mode == "auto" and (
            status.startswith("blocked") or len(text) < cfg.extract_min_chars
        )
        if need_upgrade:
            up, up_status = await _crawl4ai_extract(client, url, page_timeout, cfg, query)
            if up_status == "ok" and len(up) > len(text):
                text, source = up, "crawl4ai"
    elif mode == "crawl4ai":
        text, status = await _crawl4ai_extract(client, url, page_timeout, cfg, query)
        source = "crawl4ai" if text else "none"

    if text and store is not None:
        ttl = cfg.ttl_url_fresh if fresh else cfg.ttl_url_default
        await store.put_url(
            url_key(doc.norm_url), {"text": text, "source": source, "title": doc.title}, ttl
        )
    return text, source


# ---------------------------------------------------------------- 片段选择
_SPLIT_RE = re.compile(r"[\s,，。、;；:：!！?？/()\[\]{}\"'“”‘’|<>]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_\-.]+")


def _tokenize(text: str) -> set[str]:
    """切成可比较的词元：拉丁词整词留下，中日韩按**字符二元组**切。

    只按标点/空白切中文会把整句当成一个词元，query 与片段几乎不可能字面全同 →
    词覆盖分恒为 0，片段选择退化成「取前两块」（而中文技术页的开头往往是导航与导语）。
    二元组是零成本、无需分词模型且对中文有效的近似；夹带的型号/API 名另行单独保留。
    """
    out: set[str] = set()
    for seg in _SPLIT_RE.split((text or "").lower()):
        if not seg:
            continue
        out.update(w for w in _LATIN_RE.findall(seg) if len(w) > 1)
        chars = _CJK_RE.findall(seg)
        if len(chars) == 1:
            out.add(chars[0])
        out.update(a + b for a, b in zip(chars, chars[1:], strict=False))
    return out


def lexical_scores(query: str, passages: list[str]) -> list[float]:
    """确定性词覆盖打分（0~1）。两个用途：无 reranker 时的兜底、有 reranker 时的**预筛**。

    比「取前两块」好得多：中文技术页的开头往往是导航与导语，真正的答案常在中后段。
    """
    q_tokens = _tokenize(query)
    if not q_tokens or not passages:
        return [0.0] * len(passages)
    out: list[float] = []
    for p in passages:
        p_tokens = _tokenize(p)
        if not p_tokens:
            out.append(0.0)
            continue
        hit = len(q_tokens & p_tokens)
        # 覆盖率为主，长度做轻微加权（太短的片段信息量不足）
        out.append(round(min(1.0, hit / len(q_tokens)) * min(1.0, len(p) / 120), 4))
    return out


def _prefilter(query: str, chunks: list[str], n: int) -> list[int]:
    """从全部块里预筛出 n 个候选下标（升序，保留原文顺序便于阅读）。

    为什么不直接把所有块送给 cross-encoder：一页正文能切出几十块，而 CPU 上每一对
    都是真金白银的几百毫秒。先用零成本的词覆盖分把候选压到个位数，再让 cross-encoder
    在其中定胜负——与主漏斗「召回拉宽、重排拉准」同构。

    词覆盖分全为 0 时（例如中文 query 配纯英文页）预筛等于没信号，改为**均匀取样**：
    至少保证首、中、尾都有机会进精排，而不是集体退回开头几块。
    """
    if not chunks:
        return []
    n = max(1, min(n, len(chunks)))
    lex = lexical_scores(query, chunks)
    ranked = sorted(range(len(chunks)), key=lambda i: (-lex[i], i))[:n]
    if all(lex[i] == 0.0 for i in ranked):
        # 均匀取样要真的覆盖到尾部：`int(j * len/n)` 的最大值是 len - len/n，
        # 也就是最后 1/n 的正文永远进不了精排——而技术页的答案恰恰常在中后段
        # （开头是导航与导语）。按 (len-1) 等分才能同时拿到首、中、尾；
        # n=1 时 step 退化成 len-1，但 j 只取 0，仍落在首块（无需分支特判）
        step = (len(chunks) - 1) / max(1, n - 1)
        ranked = sorted({round(j * step) for j in range(n)})
    return sorted(ranked)


async def pick_passages(
    query: str, doc: Doc, text: str, source: str, cfg: Settings, *, timeout: float | None = None
) -> tuple[list[str], list[float], str]:
    """正文 → 切片 → 预筛 → 片段级精排 → 每篇留 N 段。返回 (片段, 分数, passage_source)。

    任何一层不达标都退回引擎摘要，不留空片段（否则模型拿不到任何证据）。
    """
    limit = max(1, cfg.passages_per_doc)
    fallback = [_squash(doc.snippet)[: cfg.passage_chars]] if doc.snippet else []
    if not text.strip():
        return fallback, [], "snippet" if fallback else "none"

    chunks = chunk_text(text, cfg.chunk_size, cfg.chunk_overlap)
    if not chunks:
        return fallback, [], "snippet" if fallback else "none"
    if len(chunks) <= limit:
        return [c[: cfg.passage_chars] for c in chunks], [], source

    cand = _prefilter(query, chunks, max(limit, cfg.passage_candidates))
    ranked = await rerank_passages(query, [chunks[i] for i in cand], limit, cfg, timeout=timeout)
    if ranked:
        picked: list[str] = []
        scores: list[float] = []
        # ranked[:limit]：服务端一般会按 top_n 返回，但不能依赖它——多给的段直接渲染出去
        # 就等于破了「每篇只留 N 段」的输出预算约定
        for pos, s in ranked[:limit]:
            if 0 <= pos < len(cand):
                picked.append(chunks[cand[pos]][: cfg.passage_chars])
                scores.append(round(float(s), 4))
        if picked and scores and scores[0] >= cfg.passage_score_threshold:
            # 片段与分数必须同步过滤：两个数组是按位置配对的（写进 doc.passage_scores），
            # 只筛一边会让第 N 段的分数挂到第 M 段上，评测与排障都会看到错乱的归因
            kept = [
                (p, s)
                for p, s in zip(picked, scores, strict=False)
                if s >= cfg.passage_score_threshold
            ]
            if kept:
                return [p for p, _ in kept], [s for _, s in kept], source
            return picked[:1], scores[:1], source
        # 全部片段都没过阈值 → 这页大概率不回答问题，退回摘要更诚实
        # （分数照样往外报：让调用方看得见「为什么退回了摘要」）
        return fallback, scores, "snippet" if fallback else source
    # reranker 不可用 → 在预筛候选里按词覆盖分定序
    lex = lexical_scores(query, chunks)
    order = sorted(cand, key=lambda i: -lex[i])[:limit]
    return (
        [chunks[i][: cfg.passage_chars] for i in order],
        [lex[i] for i in order],
        source,
    )


# ---------------------------------------------------------------- 批量抽取
async def _extract_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    doc: Doc,
    query: str,
    cfg: Settings,
    store: AsyncStore | None,
    outcome: ExtractOutcome,
    fresh: bool,
    page_timeout: float,
    passage_timeout: float,
) -> None:
    async with sem:
        try:
            text, source = await fetch_text(
                client, doc, query, cfg, store, fresh=fresh, timeout=page_timeout
            )
            passages, scores, final_source = await pick_passages(
                query, doc, text, source, cfg, timeout=passage_timeout
            )
        except Exception as e:  # noqa: BLE001 —— 单页失败只降级该页
            outcome.errors.append(f"{doc.norm_url[:60]}: {type(e).__name__}"[:120])
            passages, scores, final_source = (
                [_squash(doc.snippet)[: cfg.passage_chars]],
                [],
                "snippet",
            )
        doc.passages = passages
        doc.passage_scores = scores
        doc.passage_source = final_source
        outcome.note(final_source)
        if final_source in ("trafilatura", "crawl4ai", "cache"):
            outcome.extracted += 1


async def extract_docs(
    query: str,
    docs: list[Doc],
    cfg: Settings,
    store: AsyncStore | None = None,
    *,
    fresh: bool = False,
    budget: float | None = None,
    page_timeout: float | None = None,
) -> ExtractOutcome:
    """对 top-N 篇抽正文并选片段；其余篇保持引擎摘要。

    `budget` / `page_timeout` 由编排层按硬超时剩余时间下发（None = 用配置值）。
    """
    t0 = time.monotonic()
    out = ExtractOutcome()
    if (cfg.extractor or "").lower() == "off" or not docs:
        out.status = "disabled"
        out.elapsed_ms = int((time.monotonic() - t0) * 1000)
        return out

    total_budget = budget if budget and budget > 0 else cfg.extract_total_budget
    page_to = min(page_timeout or cfg.extract_page_timeout, total_budget)
    passage_to = min(cfg.passage_rerank_timeout, max(1.0, total_budget / 2))

    targets = docs[: max(1, cfg.extract_top_n)]
    rest = docs[max(1, cfg.extract_top_n) :]
    out.attempted = len(targets)
    for d in rest:  # 未抽取的篇目：摘要即片段（不留空）
        if d.snippet:
            d.passages = [_squash(d.snippet)[: cfg.passage_chars]]
            d.passage_source = "snippet"
            out.note("snippet")

    sem = asyncio.Semaphore(max(1, cfg.extract_concurrency))
    limits = httpx.Limits(max_connections=cfg.extract_concurrency + 4, max_keepalive_connections=4)
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    async with httpx.AsyncClient(
        limits=limits, headers=headers, follow_redirects=True, timeout=page_to
    ) as client:
        tasks = [
            asyncio.create_task(
                _extract_one(client, sem, d, query, cfg, store, out, fresh, page_to, passage_to)
            )
            for d in targets
        ]
        done, pending = await asyncio.wait(tasks, timeout=total_budget)
        for t in pending:
            t.cancel()
        if pending:
            out.errors.append(f"{len(pending)} 页抽取超预算被取消")
            for d, t in zip(targets, tasks, strict=False):
                if t in pending and not d.passages and d.snippet:
                    d.passages = [_squash(d.snippet)[: cfg.passage_chars]]
                    d.passage_source = "snippet"
                    out.note("snippet")

    out.elapsed_ms = int((time.monotonic() - t0) * 1000)
    if out.extracted == 0:
        out.status = "snippets_only"
    elif out.extracted < out.attempted:
        out.status = "partial"
    else:
        out.status = "ok"
    return out


async def fetch_page(
    url: str, question: str, max_chars: int, cfg: Settings, store: AsyncStore | None = None
) -> dict[str, Any]:
    """`web_fetch` / REST `/fetch` 的实现：单页正文抽取 + 针对 question 的片段精选。"""
    t0 = time.monotonic()
    norm = normalize_url(url)
    doc = Doc(url=url, norm_url=norm or url, site=site_label(norm or url))
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=cfg.extract_page_timeout
    ) as client:
        text, source = await fetch_text(client, doc, question, cfg, store)
    if not text:
        return {
            "url": url,
            "norm_url": doc.norm_url,
            "available": False,
            "message": "正文抽取失败（页面被拦截、非 HTML 或抽取器不可用）",
            "source": "none",
            "passages": [],
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }
    limit = max_chars if 0 < max_chars <= cfg.max_chars_hard_cap else cfg.max_chars_default
    if question.strip():
        passages, scores, _ = await pick_passages(question, doc, text, source, cfg)
    else:
        passages, scores = [], []
    chunks = chunk_text(text, cfg.chunk_size, cfg.chunk_overlap)
    body = "\n\n".join(passages) if passages else "\n\n".join(chunks)
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return {
        "url": url,
        "norm_url": doc.norm_url,
        "site": doc.site,
        "available": True,
        "source": source,
        "chars_total": len(text),
        "chunks": len(chunks),
        "passages": passages,
        "passage_scores": scores,
        "text": body,
        "truncated": len(text) > len(body),
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
