"""查询理解层单测：分路、时效意图、常青/技术判定、显著短语挑选。

多路互补的收益全部建立在「每一路都真的与别人不同」上：如果 phrase 路退化成原查询、
keyword 路把实体词也当虚词丢了，那多出来的召回只是同一批结果重复计费。
所以这里断言的是**路间差异**与**触发条件**，而不是 jieba 的具体切分结果
（分词粒度会随词典版本变，锁死它等于把测试变成脆弱断言）。
"""

import re

from websearch.config import Settings
from websearch.understand import (
    build_routes,
    detect_time_range,
    is_evergreen,
    is_fresh_intent,
    is_tech_query,
)

_CFG = Settings(_env_file=None)


def _names(routes: list[object]) -> list[str]:
    return [r.name for r in routes]  # type: ignore[attr-defined]


def _by_name(routes: list[object], name: str) -> object:
    return next(r for r in routes if r.name == name)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 基本分路
def test_empty_query_yields_no_routes() -> None:
    """空查询不产路：让上层如实报「query 不能为空」，而不是拿空串去打引擎。"""
    assert build_routes("", settings=_CFG) == []
    assert build_routes("   ", settings=_CFG) == []


def test_original_route_always_present_and_first() -> None:
    """original 是保底路，恒定第一（RRF 权重也最高）。"""
    routes = build_routes("向量数据库", settings=_CFG)
    assert routes[0].name == "original"
    assert routes[0].query == "向量数据库"
    assert routes[0].weight == _CFG.weight_map["original"]


def test_route_count_never_exceeds_four() -> None:
    """工具预算与召回预算都按 ≤4 路设计：一个同时命中所有触发条件的查询也不许超。"""
    q = '帮我查一下 "BGE-Reranker-v2-m3" 最新 API 文档 与 github 上的参数说明'
    routes = build_routes(q, settings=_CFG)
    assert len(routes) <= 4
    assert len(set(_names(routes))) == len(routes)  # 路名不重复（RRF 按路名索引权重）


def test_routes_are_deterministic() -> None:
    """同一 query 多次构造必须完全一致，否则评测数字无法复现。"""
    q = "Kubernetes 与 Docker Swarm 的调度差异"
    a = build_routes(q, settings=_CFG)
    b = build_routes(q, settings=_CFG)
    assert [(r.name, r.query, r.weight, r.time_range, r.categories) for r in a] == [
        (r.name, r.query, r.weight, r.time_range, r.categories) for r in b
    ]


def test_whitespace_collapsed_in_query() -> None:
    routes = build_routes("  向量   数据库\n原理 ", settings=_CFG)
    assert routes[0].query == "向量 数据库 原理"


# ---------------------------------------------------------------- phrase 路
def test_phrase_route_uses_quoted_span_verbatim() -> None:
    """用户已经打了引号 → 尊重它，引号内内容原样做精确匹配。"""
    routes = build_routes('关于 "bge-reranker-v2-m3" 的参数量说明', settings=_CFG)
    phrase = _by_name(routes, "phrase")
    assert '"bge-reranker-v2-m3"' in phrase.query  # type: ignore[attr-defined]


def test_phrase_span_prefers_versioned_entity_over_chinese_run() -> None:
    """含数字/连字符的拉丁串更像型号或版本号，比中文长串更值得精确匹配。"""
    routes = build_routes("如何在生产环境中把 Kubernetes 升级到 v1.29 版本", settings=_CFG)
    quoted = re.findall(r'"([^"]+)"', _by_name(routes, "phrase").query)  # type: ignore[attr-defined]
    assert quoted and quoted[0] == "v1.29"


def test_no_phrase_route_when_it_would_duplicate_original() -> None:
    """查询本身就是一个引号短语 → phrase 路与 original 完全同构，必须不出。

    同构路的代价不是「多花一次调用」这么轻：RRF 会给同一份排名投两票，
    等于把 original 路的票权从 1.0 抬到 1.9，融合结果被无声扭曲。
    """
    routes = build_routes('"vector database"', settings=_CFG)
    assert "phrase" not in _names(routes)
    assert routes[0].query == '"vector database"'  # 用户打的引号原样保留，不替他做主


# ---------------------------------------------------------------- keyword 路
def test_keyword_route_drops_chinese_stopwords() -> None:
    """虚词/疑问词必须被丢掉：带着「什么是」「的区别」去打关键词检索只会召回噪声。"""
    routes = build_routes("什么是向量数据库以及它和关系型数据库的区别", settings=_CFG)
    kw = _by_name(routes, "keyword").query  # type: ignore[attr-defined]
    for stop in ("什么是", "以及", "的区别"):
        assert stop not in kw
    assert "向量" in kw and "数据库" in kw  # 实词一个都不能丢


def test_keyword_route_keeps_latin_entities() -> None:
    """中英混查时实体词是判别力最强的部分，关键词路必须完整保留。"""
    routes = build_routes("PostgreSQL 的 vacuum 机制是怎么工作的", settings=_CFG)
    kw = _by_name(routes, "keyword").query  # type: ignore[attr-defined]
    assert "PostgreSQL" in kw
    assert "vacuum" in kw


def test_keyword_route_not_emitted_when_degenerate() -> None:
    """关键词化后与原查询相同（或只剩一个词）→ 这一路没有信息增益，不出。"""
    routes = build_routes("kubernetes", settings=_CFG)
    assert "keyword" not in _names(routes)


def test_keyword_route_dedupes_tokens() -> None:
    routes = build_routes("向量数据库 与 向量数据库 的索引结构", settings=_CFG)
    kw = _by_name(routes, "keyword").query  # type: ignore[attr-defined]
    assert kw.split().count("向量数据库") <= 1


# ---------------------------------------------------------------- extra 路
def test_extra_route_is_time_range_news_when_recency_detected() -> None:
    """时效意图优先占掉 extra 名额：news 类目 + time_range 收窄。"""
    routes = build_routes("最近有什么关于向量数据库的新进展", settings=_CFG)
    extra = _by_name(routes, "extra")
    assert extra.time_range == "month"  # type: ignore[attr-defined]
    assert extra.categories == "news"  # type: ignore[attr-defined]


def test_extra_route_is_english_entity_for_mixed_query() -> None:
    """无时效信号时，中英混查走英文实体路捞英文原始资料。"""
    routes = build_routes("BGE-Reranker-v2-m3 的参数量是多少", settings=_CFG)
    extra = _by_name(routes, "extra")
    assert "BGE-Reranker-v2-m3" in extra.query  # type: ignore[attr-defined]
    assert not any("\u4e00" <= c <= "\u9fff" for c in extra.query)  # type: ignore[attr-defined]


def test_extra_route_adds_it_category_for_tech_query() -> None:
    """技术查询追加 it 类目：实测 categories=it 会把 github/stackoverflow/mdn 拉进召回。"""
    routes = build_routes("docker compose 的 depends_on 健康检查配置", settings=_CFG)
    tech_routes = [r for r in routes if "it" in r.categories.split(",")]
    assert tech_routes, f"技术查询应至少有一路带 it 类目，实际 {[r.categories for r in routes]}"


def test_extra_route_absent_for_plain_non_tech_query() -> None:
    """三个触发条件均不成立时不硬凑第四路：extra 名额只用在真正缺的互补性上。

    前提一并断言：否则哪天这个查询变得「像技术词」了，测试会在错误的原因上通过。
    """
    q = "上海迪士尼乐园的门票价格"  # 纯中文、无拉丁实体、无时效词、非技术词
    assert not is_tech_query(q)
    assert not is_fresh_intent(q, "auto")
    routes = build_routes(q, settings=_CFG)
    assert "extra" not in _names(routes)
    assert routes[0].name == "original"  # 保底路仍在


# ---------------------------------------------------------------- site 限定
def test_site_filter_appended_to_every_route() -> None:
    """site: 必须加到每一路：只加在 original 上等于另外三路把限定条件丢了。"""
    routes = build_routes("向量数据库索引结构", site="github.com", settings=_CFG)
    assert len(routes) >= 2
    for r in routes:
        assert r.query.endswith("site:github.com")


# ---------------------------------------------------------------- 意图判定
def test_detect_time_range_explicit_freshness_wins() -> None:
    """显式 freshness 压过自动检测：调用方说了 week，就不要因为句中有「最新」而改成 month。"""
    assert detect_time_range("最新的进展", "week") == "week"
    assert detect_time_range("最新的进展", "any") == ""
    assert detect_time_range("最新的进展", "auto") == "month"


def test_detect_time_range_granularity() -> None:
    assert detect_time_range("今天发布的版本", "auto") == "day"
    assert detect_time_range("本周的动态", "auto") == "week"
    assert detect_time_range("最近有什么新闻", "auto") == "month"
    assert detect_time_range("向量数据库的原理", "auto") == ""


def test_is_fresh_intent_covers_explicit_and_detected() -> None:
    assert is_fresh_intent("任意内容", "day") is True
    assert is_fresh_intent("任意内容", "week") is True
    assert is_fresh_intent("任意内容", "month") is True  # 检测出 month 也算时效型
    assert is_fresh_intent("向量数据库原理", "auto") is False


def test_is_evergreen_matches_doc_like_queries() -> None:
    """常青 = 答案不随时间变（文档/规范/原理/参数）→ 缓存可以放很久。"""
    for q in ("PostgreSQL 官方文档", "HTTP/2 协议规范", "SDK 安装教程"):
        assert is_evergreen(q), q
    assert not is_evergreen("今天的股市行情")


def test_fresh_and_evergreen_can_co_match() -> None:
    """「最新 API 文档」同时命中时效与常青。

    谁胜不在这一层裁决：本模块只如实报告两个信号，TTL 优先级（时效 > 常青 > 缺省）
    由 cache.query_ttl 裁决，那里有独立断言（tests/test_cache.py）。
    """
    q = "最新的 API 文档"
    assert is_evergreen(q) and is_fresh_intent(q, "auto")


def test_is_evergreen_covers_definitional_queries() -> None:
    """定义型是常青的最典型形态：两种问法都得认出来，否则 TTL 会分成两档。"""
    assert is_evergreen("什么是向量数据库")
    assert is_evergreen("向量数据库是什么")


def test_is_tech_query_positive_and_negative() -> None:
    for q in ("kubernetes 部署报错", "python 依赖库版本", "docker 配置"):
        assert is_tech_query(q), q
    assert not is_tech_query("今天北京天气如何")
