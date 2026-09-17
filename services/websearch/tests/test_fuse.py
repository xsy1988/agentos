"""融合层单测：加权 RRF 的数学、近重复合并、tie-break 与 route_agreement。

RRF 是整条流水线里唯一「凭排名投票」的地方，算错就是静默的质量塌方——
所以这里全部用手算对照值断言，不写「跑通即可」的空测试。

    score(d) = Σ_r  w_r / (k + rank_r(d))        k = 60

注意：路权重取自 `cfg.weight_map`（按**路名**索引），不是 `Route.weight` 字段——
后者只用于查询理解阶段自报「这一路有多重要」。
"""

import pytest

from websearch.config import Settings
from websearch.fuse import FusionResult, rerank_shift, rrf_fuse
from websearch.normalize import normalize_url, site_label
from websearch.recall import RouteResult
from websearch.types import Doc
from websearch.understand import Route


def _cfg(**kw: object) -> Settings:
    """确定性配置：不读 .env、不读环境变量，测试值就是断言值。"""
    base: dict[str, object] = {
        "rrf_k": 60,
        "route_weights": "original:1.0,keyword:0.8",
        "candidate_window": 50,
    }
    base.update(kw)  # 调用方覆盖默认值，而不是与它冲突
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _doc(url: str, title: str = "") -> Doc:
    norm = normalize_url(url)
    return Doc(url=url, norm_url=norm, title=title, site=site_label(norm))


def _route(name: str, urls: list[str]) -> RouteResult:
    return RouteResult(route=Route(name=name, query=name), docs=[_doc(u) for u in urls])


def test_rrf_scores_match_hand_computation() -> None:
    """手算对照：d2 被两路共同召回，分数必须高于只被一路排在第一的 d1。"""
    routes = [
        _route("original", ["https://a.com/1", "https://b.com/2"]),
        _route("keyword", ["https://b.com/2", "https://c.com/3"]),
    ]
    out = rrf_fuse(routes, _cfg())
    scores = {d.norm_url: d.score for d in out.window}

    # d1 = 1.0/(60+1)；d2 = 1.0/(60+2) + 0.8/(60+1)；d3 = 0.8/(60+2)
    assert scores["https://a.com/1"] == pytest.approx(1.0 / 61, rel=1e-9)
    assert scores["https://b.com/2"] == pytest.approx(1.0 / 62 + 0.8 / 61, rel=1e-9)
    assert scores["https://c.com/3"] == pytest.approx(0.8 / 62, rel=1e-9)
    assert [d.norm_url for d in out.window] == [
        "https://b.com/2",
        "https://a.com/1",
        "https://c.com/3",
    ]


def test_rrf_records_per_route_rank() -> None:
    """routes 字典是 route_agreement 与消融分析的数据源，必须记到每一路的真实排名。"""
    routes = [
        _route("original", ["https://a.com/1", "https://b.com/2"]),
        _route("keyword", ["https://b.com/2"]),
    ]
    out = rrf_fuse(routes, _cfg())
    by_url = {d.norm_url: d for d in out.window}
    assert by_url["https://b.com/2"].routes == {"original": 2, "keyword": 1}
    assert by_url["https://a.com/1"].routes == {"original": 1}


def test_same_url_in_one_route_keeps_best_rank() -> None:
    """同一 URL 在同一路里出现两次（分页重叠）→ 只留更靠前的排名，不重复计票。"""
    rr = RouteResult(
        route=Route(name="original", query="q"),
        docs=[
            _doc("https://a.com/1"),
            _doc("https://b.com/2"),
            _doc("https://a.com/1?utm_source=news"),  # 归一化后与第一条同 URL
        ],
    )
    out = rrf_fuse([rr], _cfg())
    assert len(out.window) == 2
    assert out.window[0].routes == {"original": 1}
    assert out.stats["raw"] == 3  # 原始条数如实记录，去重后才是 2
    assert out.stats["unique_urls"] == 2


def test_route_agreement_counts_multi_route_docs_in_top10() -> None:
    """agreement = top10 中被 ≥2 路召回的占比（无需人工标注的融合层结构指标）。"""
    routes = [
        _route("original", ["https://a.com/1", "https://b.com/2", "https://c.com/3"]),
        _route("keyword", ["https://b.com/2"]),
    ]
    out = rrf_fuse(routes, _cfg())
    # 排序后 top3 里只有 b 被两路共同召回 → 1/3（实现保留 4 位小数，它要直接进 JSON）
    assert out.agreement == pytest.approx(1 / 3, abs=1e-4)
    assert out.stats["route_agreement"] == out.agreement


def test_agreement_empty_window_is_zero() -> None:
    assert FusionResult().agreement == 0.0


def test_near_duplicates_merged_by_title_and_path_prefix() -> None:
    """同一篇文章的 AMP 版与原版：同标题 + 同 host 一级路径 → 合并，证据取并集。"""
    routes = [
        _route("original", ["https://example.com/blog/post"]),
        _route("keyword", ["https://example.com/blog/post/amp"]),
    ]
    # 标题相同才会被判为近重复（无标题时退化为按归一化 URL 去重）
    for rr in routes:
        for d in rr.docs:
            d.title = "深入理解向量数据库"
    routes[0].docs[0].engines = ["google"]
    routes[1].docs[0].engines = ["bing"]
    routes[1].docs[0].date = "2026-01-02"

    out = rrf_fuse(routes, _cfg())
    assert len(out.window) == 1
    merged = out.window[0]
    assert merged.engines == ["bing", "google"]  # 并集且有序（结果可复现）
    assert merged.routes == {"original": 1, "keyword": 1}
    assert merged.date == "2026-01-02"  # 元数据取更全的那一份
    assert merged.score == pytest.approx(1.0 / 61, rel=1e-9)  # 分数取高
    assert out.stats["after_dedup"] == 1


def test_different_title_not_merged() -> None:
    """标题不同 = 不同页面，即使路径前缀一样也不能合并（宁可多占一个窗口名额）。"""
    routes = [
        _route("original", ["https://example.com/blog/post-a"]),
        _route("keyword", ["https://example.com/blog/post-b"]),
    ]
    routes[0].docs[0].title = "向量数据库原理"
    routes[1].docs[0].title = "倒排索引原理"
    out = rrf_fuse(routes, _cfg())
    assert len(out.window) == 2


def test_candidate_window_caps_output() -> None:
    """窗口大小是召回上限的封顶处：超出部分直接不进重排。"""
    urls = [f"https://s{i}.com/p" for i in range(60)]
    out = rrf_fuse([_route("original", urls)], _cfg(candidate_window=50))
    assert len(out.window) == 50
    assert out.stats["window"] == 50


def test_tiebreak_prefers_more_routes_at_equal_score() -> None:
    """同分时共识路数多者优先：两路各半票 == 一路整票，但前者证据更强。"""
    cfg = _cfg(route_weights="original:1.0,keyword:0.5,phrase:0.5")
    routes = [
        _route("original", ["https://a.com/1"]),  # 1.0/61
        _route("keyword", ["https://b.com/2"]),  # 0.5/61
        _route("phrase", ["https://b.com/2"]),  # + 0.5/61 → 与 a 完全同分
    ]
    out = rrf_fuse(routes, cfg)
    a = next(d for d in out.window if d.norm_url == "https://a.com/1")
    b = next(d for d in out.window if d.norm_url == "https://b.com/2")
    assert a.score == pytest.approx(b.score, rel=1e-12)
    assert out.window[0] is b  # 两路共识排在单路之前
    assert len(b.routes) == 2


def test_empty_routes_are_skipped() -> None:
    """空路不进 route_names / route_lists（否则 agreement 与消融统计会被稀释）。"""
    empty = RouteResult(route=Route(name="phrase", query="q"), docs=[])
    out = rrf_fuse([empty, _route("original", ["https://a.com/1"])], _cfg())
    assert out.route_names == ["original"]
    assert list(out.route_lists) == ["original"]
    assert out.stats["routes"] == 1


def test_route_lists_preserve_route_order() -> None:
    """route_lists 保留每一路的原始排名序——评测 recall@50 与调试都靠它。"""
    out = rrf_fuse([_route("original", ["https://a.com/1", "https://b.com/2"])], _cfg())
    assert out.route_lists["original"] == ["https://a.com/1", "https://b.com/2"]


def test_rrf_k_controls_head_dominance() -> None:
    """k 越小头部越强势：同一组数据，k=1 时 rank1 与 rank2 的分差远大于 k=600。"""
    routes = [_route("original", ["https://a.com/1", "https://b.com/2"])]
    small = rrf_fuse(routes, _cfg(rrf_k=1))
    large = rrf_fuse(routes, _cfg(rrf_k=600))
    gap_small = small.window[0].score - small.window[1].score
    gap_large = large.window[0].score - large.window[1].score
    assert gap_small > gap_large > 0


def test_rerank_shift_measures_position_change() -> None:
    """精排到底做了多少事：位次变化绝对值之和，0 = 完全认同 RRF 序。"""
    before = [_doc("https://a.com/1"), _doc("https://b.com/2"), _doc("https://c.com/3")]
    assert rerank_shift(before, list(before)) == 0
    swapped = [before[1], before[0], before[2]]
    assert rerank_shift(before, swapped) == 2  # 0↔1 各挪一位


def test_rerank_shift_handles_newcomer() -> None:
    """精排后出现 RRF 序里没有的 URL（理论上不该发生）→ 按「排在末尾」计位移，不炸。"""
    before = [_doc("https://a.com/1")]
    after = [_doc("https://z.com/9")]
    assert rerank_shift(before, after) == 1
