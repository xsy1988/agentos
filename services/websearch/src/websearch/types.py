"""流水线内部数据结构：各阶段之间只经这些类型传递（便于消融、评测与序列化）。"""

from dataclasses import dataclass, field
from typing import Any

# 全链路阶段名（REST ?stages= 消融按此顺序裁剪）
STAGES: tuple[str, ...] = ("understand", "recall", "fusion", "rerank", "extract")


@dataclass
class Doc:
    """一条候选结果，随流水线逐层富化。"""

    url: str  # 引擎给的原始 URL
    norm_url: str  # 归一化 URL（去重与融合的键）
    title: str = ""
    snippet: str = ""
    date: str = ""
    site: str = ""
    engines: list[str] = field(default_factory=list)
    # 路名 → 该路内的排名（1 起）。既是 RRF 输入，也是 route_agreement 的数据源
    routes: dict[str, int] = field(default_factory=dict)
    score: float = 0.0  # 加权 RRF 融合分
    rerank_score: float | None = None  # cross-encoder 分数（sigmoid 0~1）
    passages: list[str] = field(default_factory=list)  # 片段级精排后交出的正文段
    passage_source: str = "none"  # crawl4ai|trafilatura|snippet|cache|none
    passage_scores: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "norm_url": self.norm_url,
            "title": self.title,
            "snippet": self.snippet,
            "date": self.date,
            "site": self.site,
            "engines": self.engines,
            "routes": self.routes,
            "score": round(self.score, 6),
            "rerank_score": self.rerank_score,
            "passages": self.passages,
            "passage_source": self.passage_source,
        }


@dataclass
class StageTiming:
    name: str
    elapsed_ms: int
    detail: str = ""


@dataclass
class SearchParams:
    """一次搜索的全部输入。cache key 由其中「影响结果」的字段构成。"""

    query: str
    max_results: int = 8
    extract: bool = True
    freshness: str = "auto"  # auto|day|week|month|any
    site: str = ""
    lang: str = ""
    max_chars: int = 6000
    # 消融开关：只跑到某个阶段（评测用，不进 MCP 工具面）
    stages: tuple[str, ...] = STAGES

    def stage_enabled(self, name: str) -> bool:
        return name in self.stages

    def last_stage(self) -> str:
        return next((s for s in reversed(STAGES) if s in self.stages), "recall")


@dataclass
class SearchResult:
    params: SearchParams
    docs: list[Doc] = field(default_factory=list)  # 最终输出（已排序、已截断）
    window: list[Doc] = field(default_factory=list)  # 融合后候选窗口（recall@50 的数据源）
    route_names: list[str] = field(default_factory=list)
    route_lists: dict[str, list[str]] = field(default_factory=dict)  # 路名 → 归一化 URL 序
    timings: list[StageTiming] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)  # rerank/extract/cache/degraded/truncated
    errors: list[str] = field(default_factory=list)
    unresponsive_engines: list[str] = field(default_factory=list)
    available: bool = True
    message: str = ""
    elapsed_ms: int = 0
    output_chars: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    def timing_of(self, name: str) -> int:
        return next((t.elapsed_ms for t in self.timings if t.name == name), 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.params.query,
            "available": self.available,
            "message": self.message,
            "results": [d.to_dict() for d in self.docs],
            "window_size": len(self.window),
            "window_urls": [d.norm_url for d in self.window],
            "routes": self.route_names,
            "route_lists": self.route_lists,
            "timings": [
                {"stage": t.name, "ms": t.elapsed_ms, "detail": t.detail} for t in self.timings
            ],
            "flags": self.flags,
            "errors": self.errors,
            "unresponsive_engines": self.unresponsive_engines,
            "elapsed_ms": self.elapsed_ms,
            "output_chars": self.output_chars,
            "stats": self.stats,
        }
