"""召回：SearXNG 并发多路。

每路一次（或多次分页）SearXNG 调用——SearXNG 内部已聚合 google/bing/duckduckgo/
wikipedia 等引擎，路间差异来自**检索式**而非引擎集，这正是互补的来源。

纪律：单路失败/超时只影响该路，记入 errors；召回总预算用 asyncio.wait 收割已完成的路，
超时未回的路直接取消（绝不为了等一路而拖垮整条查询）。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from websearch.config import Settings
from websearch.normalize import normalize_url, site_label
from websearch.types import Doc, SearchParams
from websearch.understand import Route

# 搜索引擎自己的结果页/跳转页：召回进来只会浪费窗口名额
_JUNK_HOST_MARKERS = (
    "google.com/search",
    "bing.com/search",
    "duckduckgo.com/?",
    "search.yahoo.com",
    "baidu.com/s?",
    "so.com/s?",
    "sogou.com/web",
    "searx.",
)

_LANG_MAP = {"zh": "zh-CN", "en": "en-US", "ja": "ja-JP", "all": "all"}


@dataclass
class RouteResult:
    route: Route
    docs: list[Doc] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0
    unresponsive: list[str] = field(default_factory=list)
    raw_count: int = 0


@dataclass
class RecallOutcome:
    results: list[RouteResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unresponsive_engines: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    raw_count: int = 0

    @property
    def ok_routes(self) -> list[RouteResult]:
        return [r for r in self.results if r.docs]

    @property
    def contributing_engines(self) -> list[str]:
        """真正交出了候选的引擎（并集）。

        为什么不直接用 `unresponsive_engines` 反推：那个字段只说「谁报错了」，
        而实测最坑的引擎不报错——本机 bing 对中文查询稳定返回 10 条**无关**结果
        （查「什么是向量数据库」得 CRAN 下载页），它永远不会出现在 unresponsive 里。
        「有几个引擎真的在出数」是效果侧信号，比「几个引擎挂了」靠得住：
        输出层用它判断能不能对模型说「没搜到」（只有一两路在出数时那句话不成立）。
        """
        return sorted({e for r in self.results for d in r.docs for e in d.engines})


def _is_junk(url: str) -> bool:
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return True
    host = urlsplit(low).hostname or ""
    if not host or "." not in host:
        return True
    return any(m in low for m in _JUNK_HOST_MARKERS)


def _parse_published(item: dict[str, Any]) -> str:
    """SearXNG publishedDate 可能是 ISO 串、None 或已解析对象。"""
    val = item.get("publishedDate")
    if not val:
        return ""
    if isinstance(val, str):
        return val[:10]
    return str(getattr(val, "date", lambda: val)())[:10]


def _parse_unresponsive(raw: Any) -> list[str]:
    """兼容三种形态：["google"]、[["google"]]、[["duckduckgo", "timeout"]]（新版带原因）。"""
    out: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (list, tuple)) and item:
            out.append(str(item[0]))  # 只取引擎名，原因（timeout/…）不当引擎用
    return out


def _to_docs(items: list[dict[str, Any]], route: Route, limit: int) -> tuple[list[Doc], int]:
    """引擎结果 → 路内有序 Doc 列表（归一化 + 路内去重 + 垃圾过滤 + 排名编号）。"""
    seen: dict[str, Doc] = {}
    order: list[str] = []
    raw = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or _is_junk(url):
            continue
        raw += 1
        norm = normalize_url(url)
        if not norm or _is_junk(norm):
            continue
        cur = seen.get(norm)
        engines = [str(e) for e in (item.get("engines") or [item.get("engine")]) if e]
        if cur is not None:
            cur.engines = sorted({*cur.engines, *engines})
            continue
        doc = Doc(
            url=url,
            norm_url=norm,
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("content") or "").strip(),
            date=_parse_published(item),
            site=site_label(norm),
            engines=sorted(set(engines)),
            routes={route.name: len(order) + 1},
        )
        seen[norm] = doc
        order.append(norm)
        if len(order) >= limit:
            break
    return [seen[k] for k in order], raw


async def _fetch_route(
    client: httpx.AsyncClient,
    route: Route,
    params: SearchParams,
    cfg: Settings,
    route_timeout: float,
) -> RouteResult:
    """单路召回（可分页）。任何异常都收敛成 RouteResult.error，不上抛。"""
    t0 = time.monotonic()
    base = cfg.searxng_url.rstrip("/")
    engines = cfg.engine_list
    query_args: dict[str, Any] = {"q": route.query, "format": "json", "safesearch": 0}
    # 实测：同时传 categories 与 engines 时 SearXNG 以 categories 为准（engines 被忽略），
    # 所以两者只能选一：配了引擎白名单就不发 categories，否则走路由自带的类目
    if engines:
        query_args["engines"] = ",".join(engines)
    else:
        query_args["categories"] = route.categories or "general"
    if route.time_range:
        query_args["time_range"] = route.time_range
    if params.lang:
        query_args["language"] = _LANG_MAP.get(params.lang.lower(), params.lang)

    items: list[dict[str, Any]] = []
    unresponsive: list[str] = []
    try:
        for page in range(1, max(1, cfg.recall_pages) + 1):
            resp = await client.get(
                f"{base}/search",
                params={**query_args, "pageno": page},
                timeout=route_timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"SearXNG HTTP {resp.status_code}: {resp.text[:120]}")
            data = resp.json()
            page_items = data.get("results") or []
            unresponsive.extend(_parse_unresponsive(data.get("unresponsive_engines")))
            items.extend(page_items)
            if len(page_items) < 5:
                break  # 该路已无更多结果
        docs, raw = _to_docs(items, route, cfg.recall_per_route_limit)
        return RouteResult(
            route=route,
            docs=docs,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            unresponsive=sorted(set(unresponsive)),
            raw_count=raw,
        )
    except Exception as e:  # noqa: BLE001 —— 单路失败不拖垮整条查询
        return RouteResult(
            route=route,
            error=f"{type(e).__name__}: {e}"[:200],
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            unresponsive=sorted(set(unresponsive)),
        )


async def recall(
    routes: list[Route], params: SearchParams, cfg: Settings, *, budget: float | None = None
) -> RecallOutcome:
    """并发跑完全部路，按总预算收割。

    `budget` 由编排层根据硬超时剩余时间下发（None = 用配置值）；单路超时同时被总预算封顶，
    避免出现「单路 8s > 总预算 3s」这种永不可能完成的配置。
    """
    t0 = time.monotonic()
    out = RecallOutcome()
    if not routes:
        out.errors.append("无可执行检索式")
        return out
    total_budget = budget if budget and budget > 0 else cfg.recall_total_budget
    route_timeout = min(cfg.recall_route_timeout, total_budget)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=8)
    async with httpx.AsyncClient(
        limits=limits, headers={"User-Agent": "websearch-pipeline/0.1"}, follow_redirects=False
    ) as client:
        tasks = [
            asyncio.create_task(_fetch_route(client, r, params, cfg, route_timeout)) for r in routes
        ]
        done, pending = await asyncio.wait(tasks, timeout=total_budget)
        for t in pending:
            t.cancel()
        by_task = {id(t): t for t in done}
        for t in tasks:
            if id(t) in by_task:
                exc = t.exception()
                if exc is not None:  # 理论上不会到这（_fetch_route 已兜异常）
                    out.errors.append(f"{type(exc).__name__}: {exc}"[:200])
                else:
                    out.results.append(t.result())
            else:
                out.errors.append(f"召回超预算（{total_budget:.1f}s）")
    # 保持与 routes 相同的顺序（便于对照 route_lists）
    order = {r.name: i for i, r in enumerate(routes)}
    out.results.sort(key=lambda r: order.get(r.route.name, 99))
    for r in out.results:
        if r.error:
            out.errors.append(f"路 {r.route.name}: {r.error}")
        out.unresponsive_engines.extend(r.unresponsive)
        out.raw_count += r.raw_count
    out.unresponsive_engines = sorted(set(out.unresponsive_engines))
    out.elapsed_ms = int((time.monotonic() - t0) * 1000)
    return out
