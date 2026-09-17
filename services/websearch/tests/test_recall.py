"""召回层单测：SearXNG 请求契约、响应解析、垃圾过滤、单路失败隔离、总预算收割。

召回决定上限——后面所有层都只能在召回给的那批候选里做文章。而这一层的故障**全都不报错**：
`format=json` 少传就是 403、`categories` 与 `engines` 同传则后者被忽略、
`publishedDate` 换个形态就解析成空——表现都只是「结果变差了」。
所以这里把请求参数与响应形态逐条钉死。
"""

import asyncio
import time
from datetime import datetime
from typing import Any

import pytest

from websearch.config import Settings
from websearch.recall import _is_junk, _parse_published, _parse_unresponsive, recall
from websearch.types import SearchParams
from websearch.understand import Route


def _item(
    url: str,
    *,
    title: str = "标题",
    content: str = "摘要",
    date: Any = None,
    engines: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url, "title": title, "content": content}
    if date is not None:
        out["publishedDate"] = date
    out["engines"] = engines if engines is not None else ["google"]
    return out


def _one(url: str = "https://example.com/a") -> Any:
    return lambda params: (200, {"results": [_item(url)]})


def _cfg(fake: Any, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "searxng_url": fake.cfg.searxng_url,
        "recall_pages": 1,
        "recall_per_route_limit": 30,
        "recall_route_timeout": 5.0,
        "recall_total_budget": 8.0,
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


def _route(name: str = "original", query: str = "向量数据库", **kw: Any) -> Route:
    return Route(name=name, query=query, **kw)


# ---------------------------------------------------------------- 请求契约
def test_request_contract(fake_searxng: Any) -> None:
    """`format=json` 是头号坑：SearXNG 默认只开 html，缺这个参数直接 403。"""
    with fake_searxng(_one()) as fake:
        cfg = _cfg(fake)
        asyncio.run(recall([_route()], SearchParams(query="向量数据库"), cfg))
        req = fake.last
    assert req["_path"] == "/search"
    assert req["format"] == "json"
    assert req["q"] == "向量数据库"
    assert req["safesearch"] == "0"
    assert req["categories"] == "general"
    assert req["pageno"] == "1"
    assert "engines" not in req


def test_engine_whitelist_replaces_categories(fake_searxng: Any) -> None:
    """实测：两者同传时 SearXNG 以 categories 为准，engines 被忽略。

    所以配了引擎白名单就必须**不发** categories，否则白名单形同虚设——
    表现是「我明明限定了 bing，结果里还是 google」，查半天查不到配置上。
    """
    with fake_searxng(_one()) as fake:
        cfg = _cfg(fake, searxng_engines="bing, duckduckgo")
        asyncio.run(recall([_route("extra", "q", categories="news")], SearchParams(query="q"), cfg))
        req = fake.last
    assert req["engines"] == "bing,duckduckgo"
    assert "categories" not in req


def test_route_categories_and_time_range_are_forwarded(fake_searxng: Any) -> None:
    with fake_searxng(_one()) as fake:
        cfg = _cfg(fake)
        asyncio.run(
            recall(
                [_route("extra", "最新进展", categories="news", time_range="month")],
                SearchParams(query="最新进展"),
                cfg,
            )
        )
        req = fake.last
    assert req["categories"] == "news"
    assert req["time_range"] == "month"


def test_time_range_absent_when_route_has_none(fake_searxng: Any) -> None:
    """不该传空 time_range：SearXNG 收到空值会当成非法参数，整路 400。"""
    with fake_searxng(_one()) as fake:
        asyncio.run(recall([_route()], SearchParams(query="q"), _cfg(fake)))
        assert "time_range" not in fake.last


def test_language_mapping(fake_searxng: Any) -> None:
    """lang 是内部简写，发给 SearXNG 要换成它的 locale 形式；不认识的原样透传。"""
    with fake_searxng(_one()) as fake:
        cfg = _cfg(fake)
        asyncio.run(recall([_route()], SearchParams(query="q", lang="zh"), cfg))
        assert fake.last["language"] == "zh-CN"
        asyncio.run(recall([_route()], SearchParams(query="q", lang="klingon"), cfg))
        assert fake.last["language"] == "klingon"
        asyncio.run(recall([_route()], SearchParams(query="q"), cfg))
        assert "language" not in fake.last


# ---------------------------------------------------------------- 响应解析
def test_parses_results_into_ranked_docs(fake_searxng: Any) -> None:
    items = [
        _item(
            "https://example.com/a?utm_source=x",
            title="甲",
            content="摘要甲",
            date="2026-03-01T10:00:00",
            engines=["google", "bing"],
        ),
        _item("https://docs.example.com/b", title="乙"),
    ]
    with fake_searxng(lambda p: (200, {"results": items})) as fake:
        out = asyncio.run(recall([_route()], SearchParams(query="q"), _cfg(fake)))
    docs = out.results[0].docs
    assert [d.norm_url for d in docs] == ["https://example.com/a", "https://docs.example.com/b"]
    assert docs[0].title == "甲" and docs[0].snippet == "摘要甲"
    assert docs[0].date == "2026-03-01"  # ISO 串只取日期部分
    assert docs[0].site == "example.com"
    assert docs[0].engines == ["bing", "google"]  # 排序去重：结果必须可复现
    # 路内排名是 RRF 与 route_agreement 的输入，必须从 1 起且连续
    assert docs[0].routes == {"original": 1}
    assert docs[1].routes == {"original": 2}
    assert out.raw_count == 2


def test_junk_and_duplicate_results_are_filtered(fake_searxng: Any) -> None:
    """引擎自己的结果页、相对路径、同页变体都要在**路内**处理掉：
    留到融合层就会占掉候选窗口的名额，还会给同一个页面投两票。"""
    items = [
        "not a dict",
        _item("https://www.google.com/search?q=x"),
        _item("/relative/path"),
        _item("https://localhost/x"),
        _item("https://searx.be/search?q=x"),
        _item("https://example.com/a", engines=["google"]),
        _item("https://example.com/a?utm_source=y", engines=["bing"]),  # 同页变体
        _item("https://example.com/b"),
    ]
    with fake_searxng(lambda p: (200, {"results": items})) as fake:
        out = asyncio.run(recall([_route()], SearchParams(query="q"), _cfg(fake)))
    docs = out.results[0].docs
    assert [d.norm_url for d in docs] == ["https://example.com/a", "https://example.com/b"]
    assert docs[0].engines == ["bing", "google"]  # 变体的引擎并进来，但只占一个名次
    assert docs[1].routes == {"original": 2}  # 排名不因被过滤的条目而跳号


def test_per_route_limit_caps_docs(fake_searxng: Any) -> None:
    items = [_item(f"https://example.com/p{i}") for i in range(12)]
    with fake_searxng(lambda p: (200, {"results": items})) as fake:
        cfg = _cfg(fake, recall_per_route_limit=5)
        out = asyncio.run(recall([_route()], SearchParams(query="q"), cfg))
    docs = out.results[0].docs
    assert len(docs) == 5
    assert [d.routes["original"] for d in docs] == [1, 2, 3, 4, 5]


def test_paging_stops_when_a_page_is_short(fake_searxng: Any) -> None:
    """配了 3 页，但第 2 页只回 2 条 → 第 3 页不该再发（白等一个超时额度）。"""

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        page = int(params["pageno"])
        n = 6 if page == 1 else 2
        return 200, {"results": [_item(f"https://example.com/p{page}-{i}") for i in range(n)]}

    with fake_searxng(responder) as fake:
        cfg = _cfg(fake, recall_pages=3)
        out = asyncio.run(recall([_route()], SearchParams(query="q"), cfg))
        pages = fake.values_of("pageno")
    assert pages == ["1", "2"]
    assert len(out.results[0].docs) == 8


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["google"], ["google"]),
        ([["google"]], ["google"]),
        # 二元素形态只取引擎名；解析函数保持输入序（跨路排序由 recall 统一做）
        ([["duckduckgo", "timeout"], "brave"], ["duckduckgo", "brave"]),
        ([], []),
        (None, []),
    ],
)
def test_parse_unresponsive_handles_all_shapes(raw: Any, expected: list[str]) -> None:
    """SearXNG 各版本的 unresponsive_engines 形态不一：字符串、单元素列表、
    带原因的二元素列表。只取引擎名——把 "timeout" 当成引擎名报出去会让人查错方向。"""
    assert _parse_unresponsive(raw) == expected


def test_unresponsive_engines_aggregated_across_routes(fake_searxng: Any) -> None:
    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if params["q"] == "q1":
            return 200, {
                "results": [_item("https://a.com/1")],
                "unresponsive_engines": ["google"],
            }
        return 200, {
            "results": [_item("https://b.com/2")],
            "unresponsive_engines": [["google"], ["brave"]],
        }

    routes = [_route("original", "q1"), _route("keyword", "q2")]
    with fake_searxng(responder) as fake:
        out = asyncio.run(recall(routes, SearchParams(query="q"), _cfg(fake)))
    assert out.unresponsive_engines == ["brave", "google"]  # 跨路去重且有序


def test_contributing_engines_is_the_union_across_routes(fake_searxng: Any) -> None:
    """「谁真的交出了候选」——跨路并集、去重、有序。

    这个值供输出层判断能不能对模型说「未搜到」：unresponsive_engines 只说谁报错了，
    而实测最坑的引擎不报错（本机 bing 对中文查询稳定返回无关页）。两者必须分开看。
    """

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if params["q"] == "q1":
            return 200, {"results": [_item("https://a.com/1", engines=["sogou"])]}
        return 200, {"results": [_item("https://b.com/2", engines=["naver", "sogou"])]}

    routes = [_route("original", "q1"), _route("keyword", "q2")]
    with fake_searxng(responder) as fake:
        out = asyncio.run(recall(routes, SearchParams(query="q"), _cfg(fake)))
    assert out.contributing_engines == ["naver", "sogou"]  # 并集去重有序


def test_contributing_engines_excludes_failed_routes(fake_searxng: Any) -> None:
    """一路报错（无 docs）不往 contributing 里添任何东西；另一路的引擎照常计入。

    否则一个报错的路会把「真正在出数的引擎数」算多，让输出层误判为召回健康。
    """

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if params["q"] == "boom":
            return 500, {}
        return 200, {"results": [_item("https://b.com/2", engines=["yandex"])]}

    routes = [_route("original", "boom"), _route("keyword", "ok")]
    with fake_searxng(responder) as fake:
        out = asyncio.run(recall(routes, SearchParams(query="q"), _cfg(fake)))
    assert out.contributing_engines == ["yandex"]


def test_contributing_engines_empty_when_nothing_recalled(fake_searxng: Any) -> None:
    """全空召回 → 空列表。这正是「掐断」最硬的证据（输出层据此说降级而不是未搜到）。"""
    with fake_searxng(lambda p: (200, {"results": []})) as fake:
        out = asyncio.run(recall([_route()], SearchParams(query="q"), _cfg(fake)))
    assert out.contributing_engines == []


# ---------------------------------------------------------------- 失败隔离与预算
def test_one_route_failure_does_not_kill_the_others(fake_searxng: Any) -> None:
    """单路 403（limiter 打开或 formats 没配 json）不许拖垮整条查询。"""

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if params["q"] == "坏路":
            return 403, {"message": "Forbidden"}
        return 200, {"results": [_item("https://example.com/ok")]}

    routes = [_route("original", "坏路"), _route("keyword", "好路")]
    with fake_searxng(responder) as fake:
        out = asyncio.run(recall(routes, SearchParams(query="q"), _cfg(fake)))
    assert [r.route.name for r in out.ok_routes] == ["keyword"]
    assert len(out.results) == 2  # 失败的路也留在结果里带着 error，不静默消失
    # 错误串以**路名**定位（不是查询原文）：路名短且唯一，排障时能直接对上 route_lists
    assert any("original" in e and "403" in e for e in out.errors)


def test_total_outage_is_reported_not_raised(fake_searxng: Any) -> None:
    """SearXNG 全挂时交给编排层的是「一路都没成」的事实，而不是异常。

    编排层据此返回 available=false 的明确文案；抛异常会让整条 run 崩掉。
    """
    with fake_searxng(lambda p: (500, {"detail": "boom"})) as fake:
        out = asyncio.run(
            recall(
                [_route("original", "q"), _route("keyword", "q")],
                SearchParams(query="q"),
                _cfg(fake),
            )
        )
    assert out.ok_routes == []
    assert len(out.errors) == 2
    assert out.raw_count == 0


def test_results_keep_route_order(fake_searxng: Any) -> None:
    """结果顺序必须与 routes 一致：编排层按路名对照 route_lists，乱了就对不上账。"""

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        return 200, {"results": [_item(f"https://example.com/{params['q']}")]}

    routes = [_route("original", "q1"), _route("keyword", "q2"), _route("extra", "q3")]
    with fake_searxng(responder) as fake:
        out = asyncio.run(recall(routes, SearchParams(query="q"), _cfg(fake)))
    assert [r.route.name for r in out.results] == ["original", "keyword", "extra"]


def test_no_routes_is_reported(fake_searxng: Any) -> None:
    with fake_searxng(_one()) as fake:
        out = asyncio.run(recall([], SearchParams(query="q"), fake.cfg))
        assert fake.requests == []  # 一次 HTTP 都不该发
    assert out.errors == ["无可执行检索式"]
    assert out.ok_routes == []


def test_slow_route_cannot_drag_the_whole_recall(fake_searxng: Any) -> None:
    """总预算到点就收割：慢的那路记错误、快的照常出结果。

    这条是「全链路硬超时 25s」能成立的前提——一路卡住就整体卡住的话，
    MCP 探活会在 180s 后把这个能力摘掉。
    """

    def responder(params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if params["q"] == "慢查询":
            time.sleep(1.0)
        return 200, {"results": [_item(f"https://example.com/{params['q']}")]}

    routes = [_route("original", "慢查询"), _route("keyword", "快查询")]
    with fake_searxng(responder) as fake:
        cfg = _cfg(fake, recall_route_timeout=5.0, recall_total_budget=10.0)
        t0 = time.monotonic()
        out = asyncio.run(recall(routes, SearchParams(query="q"), cfg, budget=0.4))
        elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"总预算 0.4s 却花了 {elapsed:.1f}s：慢路把整条查询拖住了"
    assert [r.route.name for r in out.ok_routes] == ["keyword"]
    assert out.errors  # 慢路如实记账，不静默


# ---------------------------------------------------------------- 纯函数
@pytest.mark.parametrize(
    ("url", "junk"),
    [
        ("https://example.com/a", False),
        ("https://www.google.com/search?q=x", True),
        ("https://www.bing.com/search?q=x", True),
        ("https://duckduckgo.com/?q=x", True),
        ("https://searx.be/search?q=x", True),
        ("/relative/path", True),  # 非 http
        ("ftp://example.com/a", True),
        ("https://localhost/x", True),  # host 里没有点
        ("", True),
    ],
)
def test_is_junk(url: str, junk: bool) -> None:
    assert _is_junk(url) is junk


def test_parse_published_formats() -> None:
    assert _parse_published({"publishedDate": "2026-03-01T10:00:00"}) == "2026-03-01"
    assert _parse_published({"publishedDate": datetime(2026, 3, 1, 10, 0)}) == "2026-03-01"
    assert _parse_published({"publishedDate": None}) == ""
    assert _parse_published({}) == ""
