"""融合：加权 RRF（Reciprocal Rank Fusion）。

为什么不用引擎分数加权求和：各引擎 score 量纲不可比（有的归一化到 0-1，有的无上界，
有的是整数排名），任何跨源加权求和都要先归一化，而归一化本身就引入偏差。RRF 只用**排名**：

    score(d) = Σ_r  w_r / (k + rank_r(d))        k = 60（默认）

跨路共识自然胜出：一条结果被三路都排在前面，分数就高；只被一路偏爱，权重也压得住。
k 的作用是「压头部」：k 越小，rank=1 的优势越夸张；k 越大，各路越平均。
"""

from dataclasses import dataclass, field
from typing import Any

from websearch.config import Settings
from websearch.normalize import merge_near_duplicates
from websearch.recall import RouteResult
from websearch.types import Doc


@dataclass
class FusionResult:
    window: list[Doc] = field(default_factory=list)  # 候选窗口（已排序，重排的输入）
    route_names: list[str] = field(default_factory=list)
    route_lists: dict[str, list[str]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def agreement(self) -> float:
        """top10 中被 ≥2 路共同召回的占比（无需标注的融合层结构指标）。"""
        top = self.window[:10]
        if not top:
            return 0.0
        agreed = sum(1 for d in top if len(d.routes) >= 2)
        return round(agreed / len(top), 4)


def _merge_doc(cur: Doc, incoming: Doc, route: str, rank: int) -> None:
    """同一归一化 URL 在多路/多引擎出现 → 证据合并（排名取更靠前，元数据取更全）。"""
    if rank < cur.routes.get(route, 10**9):
        cur.routes[route] = rank
    cur.engines = sorted({*cur.engines, *incoming.engines})
    if not cur.title and incoming.title:
        cur.title = incoming.title
    if len(incoming.snippet) > len(cur.snippet):
        cur.snippet = incoming.snippet
    if not cur.date and incoming.date:
        cur.date = incoming.date
    if not cur.site and incoming.site:
        cur.site = incoming.site


def rrf_fuse(route_results: list[RouteResult], cfg: Settings) -> FusionResult:
    """多路有序列表 → 加权 RRF → 近重复合并 → 候选窗口。"""
    weights = cfg.weight_map
    k = max(1, cfg.rrf_k)
    by_url: dict[str, Doc] = {}
    route_names: list[str] = []
    route_lists: dict[str, list[str]] = {}
    raw = 0

    for rr in route_results:
        if not rr.docs:
            continue
        if rr.route.name not in route_names:
            route_names.append(rr.route.name)
        route_lists[rr.route.name] = [d.norm_url for d in rr.docs]
        for rank, d in enumerate(rr.docs, start=1):
            raw += 1
            cur = by_url.get(d.norm_url)
            if cur is None:
                clone = Doc(
                    url=d.url,
                    norm_url=d.norm_url,
                    title=d.title,
                    snippet=d.snippet,
                    date=d.date,
                    site=d.site,
                    engines=list(d.engines),
                    routes={rr.route.name: rank},
                )
                by_url[d.norm_url] = clone
            else:
                _merge_doc(cur, d, rr.route.name, rank)

    docs = list(by_url.values())
    for d in docs:
        d.score = sum(weights.get(r, 1.0) / (k + rank) for r, rank in d.routes.items())
    # 近重复合并（同标题 + 同 host 一级路径）：票与引擎并集，分数取高
    deduped = merge_near_duplicates(docs)
    # 排序：融合分 → 共识路数 → 最好排名（三级 tie-break，稳定可复现）
    deduped.sort(key=lambda d: (-d.score, -len(d.routes), min(d.routes.values())))
    window = deduped[: max(1, cfg.candidate_window)]

    stats = {
        "raw": raw,
        "unique_urls": len(docs),
        "after_dedup": len(deduped),
        "window": len(window),
        "routes": len(route_names),
        "route_agreement": FusionResult(window=window).agreement,
    }
    return FusionResult(
        window=window, route_names=route_names, route_lists=route_lists, stats=stats
    )


def rerank_shift(before: list[Doc], after: list[Doc]) -> int:
    """重排到底做了多少事：top10 位次变化的绝对值之和（0 = 精排完全认同 RRF 序）。"""
    old_pos = {d.norm_url: i for i, d in enumerate(before)}
    return sum(abs(old_pos.get(d.norm_url, len(before)) - i) for i, d in enumerate(after))
