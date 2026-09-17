"""重排：cross-encoder 精排客户端（BGE-Reranker-v2-m3，独立容器 CPU 推理）。

为什么重排是收益最高的一环：召回阶段（倒排/BM25/向量）都是双塔结构，query 与 document
各自独立编码；cross-encoder 把两者拼进同一个 Transformer 充分交互，因而能压掉最常见的那类
噪声——「页面上出现了所有关键词，但整篇在讲另一件事」。

纪律：精排失败/超时**只降级不失败**（回退融合序并如实标记），永不因这一层挂掉整条查询。
"""

import time
from typing import Any

import httpx

from websearch.config import Settings
from websearch.types import Doc

# 精排状态（写进 result.flags.rerank，可观测）
OK = "ok"
PARTIAL = "partial"  # 预算内只打完一部分 → 已打分的用精排序，剩余按 RRF 序补位
SKIPPED = "skipped"


def doc_text(d: Doc, limit: int) -> str:
    """送进 cross-encoder 的文档侧文本：标题 + 摘要 + URL 路径语义化。"""
    path_words = " ".join(
        p.replace("-", " ").replace("_", " ")
        for p in d.norm_url.split("/", 3)[-1].split("/")
        if p
    )
    text = f"{d.title}\n{d.snippet}\n{path_words}".strip()
    return text[:limit]


async def _post_rerank(
    query: str,
    documents: list[str],
    top_n: int,
    timeout: float,
    cfg: Settings,
    budget_s: float = 0.0,
    max_length: int = 0,
) -> list[tuple[int, float]] | None:
    """调 reranker 容器。返回 [(原索引, 分数)] 按分数降序；不可用返回 None。

    `budget_s` 是告知服务端的软预算：它会在超预算时停止打分并返回已算部分（partial），
    比客户端直接断开、一分不得好得多。

    `max_length` 按层分别下发（文档级窄、片段级宽）：CPU 上成本随 token 长度约平方增长，
    这是把全链路压进硬超时最直接的一个旋钮。
    """
    if not documents:
        return []
    url = cfg.reranker_url.rstrip("/") + "/rerank"
    body: dict[str, Any] = {"query": query, "documents": documents, "top_n": top_n}
    if budget_s > 0:
        body["budget_s"] = round(budget_s, 2)
    if max_length > 0:
        body["max_length"] = int(max_length)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body)
            if resp.status_code != 200:
                return None
            data: dict[str, Any] = resp.json()
            out = [
                (int(r["index"]), float(r["score"]))
                for r in (data.get("results") or [])
                if isinstance(r, dict) and "index" in r
            ]
            # 服务端按分数降序返回，但客户端不依赖这个约定：两边镜像各自演进，
            # 排错序不会报错，只会让「精排」静默变成「乱排」。自己再排一次：n ≤ 50
            out.sort(key=lambda pair: -pair[1])
            return out or None
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError):
        return None


async def rerank_docs(
    query: str, docs: list[Doc], cfg: Settings, *, timeout: float | None = None
) -> tuple[list[Doc], str, int]:
    """候选窗口 → 精排 top N。返回 (排序后的 docs, 状态, 耗时 ms)。

    `timeout` 由编排层按硬超时剩余时间下发（None = 用配置值）。

    只精排 RRF 头部 `rerank_candidates` 条：候选窗口 50 是**召回**的度量口径（recall@50），
    而精排只影响这 50 条里的排序——排在 20 名之后的本来也进不了 top10，不值得为它们
    多花十几秒 CPU。窗口大小不变，所以 recall@50 与消融对比都不受影响。

    分数低于 `rerank_score_threshold` 的候选会被丢掉（而不是凑满 top_n），所以返回条数
    可能少于 rerank_top_n——这是有意为之：交两条模型自己判为无关的页面给 Agent，
    比交八条干净的更糟。
    """
    t0 = time.monotonic()
    if not cfg.rerank_enabled or not docs:
        return docs[: cfg.rerank_top_n], SKIPPED, 0
    window = docs[: max(cfg.rerank_top_n, cfg.rerank_candidates)]
    budget = timeout if timeout and timeout > 0 else cfg.rerank_timeout
    server_budget = max(0.5, budget - cfg.rerank_server_slack)
    payload = [doc_text(d, cfg.rerank_doc_chars) for d in window]
    ranked = await _post_rerank(
        query,
        payload,
        # 要回**全部**已打分候选，而不是只要 top_n：低置信过滤会把垃圾条目剔掉，
        # 只有手里握着 11~20 名，被剔掉的名额才有正常候选可以顶上。
        # 服务端的 top_n 只截断响应、不影响打分成本（成本在 _score_all 里已经花完了）
        len(window),
        budget,
        cfg,
        budget_s=server_budget,
        max_length=cfg.rerank_max_length,
    )
    elapsed = int((time.monotonic() - t0) * 1000)
    if ranked is None:
        return window[: cfg.rerank_top_n], "unavailable", elapsed
    picked: list[int] = []
    out: list[Doc] = []
    for idx, score in ranked:
        if not 0 <= idx < len(window) or idx in picked:
            continue
        picked.append(idx)
        window[idx].rerank_score = round(score, 4)
        out.append(window[idx])
    if not out:
        return window[: cfg.rerank_top_n], "unavailable", elapsed
    # 低置信过滤：只剔**打完分**的候选，剔完不回填（回填只会把刚丢掉的垃圾重新塞进 top10）。
    # 因为上面要回了全部分数，被剔掉的名额自然由 11~20 名顶上
    threshold = cfg.rerank_score_threshold
    if threshold > 0:
        out = [d for d in out if (d.rerank_score or 0.0) >= threshold]
    # PARTIAL 只该表示「预算内没打完」，不能与「过滤掉了垃圾」混为一谈：
    # 后者是精排正常工作的结果，标成 partial 会让编排层误报 degraded
    scored_all = len(picked) >= len(window)
    status = OK
    if len(out) < cfg.rerank_top_n and not scored_all:
        # 宁可把未精排的 RRF 序候选补在后面，也不要少给模型几条结果。补位条目
        # **不带** rerank_score：下游（评测 nDCG、meta 的 rerank=partial）能看出哪些是真排过的
        status = PARTIAL
        for i, d in enumerate(window):
            if len(out) >= cfg.rerank_top_n:
                break
            if i not in picked:
                out.append(d)
    if not out:
        # 打完分的候选全被判无关，且没有未打分的可补 → 如实交出 0 条。
        # 上层会输出「未搜到相关的」并附上原因；塞两条模型自己判为无关的页面给 Agent，
        # 它会把那当真证据引用
        return [], OK, elapsed
    return out[: cfg.rerank_top_n], status, elapsed


async def rerank_passages(
    query: str, passages: list[str], top_n: int, cfg: Settings, *, timeout: float | None = None
) -> list[tuple[int, float]] | None:
    """片段级二次精排：query × 同一篇的候选片段 → [(索引, 分数)] 降序。

    引擎摘要常常恰好错过真正回答问题的那一段；整页正文又会用导航和广告稀释关键信息。
    正确做法是在片段级别再选一次。不可用返回 None（调用方退回摘要）。
    """
    if not passages:
        return []
    budget = timeout if timeout and timeout > 0 else cfg.passage_rerank_timeout
    return await _post_rerank(
        query,
        passages,
        top_n,
        budget,
        cfg,
        budget_s=max(0.5, budget - cfg.rerank_server_slack),
        max_length=cfg.passage_max_length,
    )


async def ping(cfg: Settings, timeout: float = 3.0) -> dict[str, Any]:
    """探活：返回 {available, model, loaded, latency_ms}（search_meta / health 用）。"""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(cfg.reranker_url.rstrip("/") + "/health")
            if resp.status_code != 200:
                return {"available": False, "reason": f"HTTP {resp.status_code}"}
            data = dict(resp.json())
            data["available"] = True
            data["latency_ms"] = int((time.monotonic() - t0) * 1000)
            return data
    except (TimeoutError, httpx.HTTPError, ValueError) as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"[:120]}
