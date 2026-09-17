"""缓存与指标层单测：key 稳定性、TTL 分档、过期边界、命中率、延迟环形缓冲。

缓存最容易出的两类事故都不报错，只是安静地给错答案：
1. **key 太宽**——把只影响渲染的参数（max_chars/max_results）也算进 key，命中率被稀释；
2. **key 太窄**——漏掉真正改变结果的参数（freshness/extract/stages），
   于是「要抽正文的查询」拿到「没抽正文」的旧结果，或「问最新」的查询命中一周前的缓存。
所以这里两侧都断言，而不只测「存进去取得出来」。
"""

import asyncio
from pathlib import Path

import pytest

from websearch import cache as cache_mod
from websearch.cache import (
    HIT_QUERY,
    LAST_UNRESPONSIVE,
    MISS_QUERY,
    AsyncStore,
    Store,
    percentile,
    query_key,
    query_ttl,
    url_key,
)
from websearch.config import Settings
from websearch.types import SearchParams

_CFG = Settings(_env_file=None)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """每个用例一个独立库文件：缓存是跨用例污染最隐蔽的来源。"""
    s = Store(tmp_path / "websearch.db")
    s.init()
    yield s
    s.close()


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch):
    """把 `_now()` 钉住再按需推进：过期逻辑用 sleep 测既慢又不稳定。"""
    state = {"t": 1_700_000_000}
    monkeypatch.setattr(cache_mod, "_now", lambda: state["t"])

    def advance(seconds: int) -> None:
        state["t"] += seconds

    return advance


# ---------------------------------------------------------------- query_key
def test_query_key_ignores_case_and_redundant_whitespace() -> None:
    """首尾空白与重复空白不改变发给引擎的检索式 → 必须命中同一条缓存。"""
    a = query_key(SearchParams(query="向量数据库"))
    b = query_key(SearchParams(query="  向量数据库 "))
    c = query_key(SearchParams(query="Vector Database"))
    d = query_key(SearchParams(query="vector database"))
    e = query_key(SearchParams(query="向量   数据库"))
    f = query_key(SearchParams(query="向量 数据库"))
    assert a == b
    assert c == d
    assert e == f  # 多个空格折成一个，与单空格版同键


def test_query_key_treats_cjk_spacing_as_significant() -> None:
    """「向量数据库」与「向量 数据库」是两条不同的检索式，不得共用缓存。

    缓存键必须等于**流水线实际发出去的东西**：understand 层只折空白不去空格，
    两者到了 SearXNG 就是不同的 q（中文分词边界也跟着变）。把它们归为一键
    看起来是「提高命中率」，实际上是拿 A 查询的结果去回答 B 查询。
    """
    assert query_key(SearchParams(query="向量数据库")) != query_key(
        SearchParams(query="向量 数据库")
    )


def test_query_key_excludes_rendering_only_params() -> None:
    """max_chars/max_results 只影响渲染：进 key 会让同一次搜索的不同展示各存一份，命中率白掉。"""
    base = query_key(SearchParams(query="向量数据库"))
    assert query_key(SearchParams(query="向量数据库", max_chars=2000)) == base
    assert query_key(SearchParams(query="向量数据库", max_results=3)) == base


def test_query_key_separates_result_changing_params() -> None:
    """这些参数任何一个变了，结果集就不同——共用缓存等于返回错的答案。"""
    base = query_key(SearchParams(query="向量数据库"))
    variants = [
        SearchParams(query="向量数据库", freshness="day"),
        SearchParams(query="向量数据库", site="github.com"),
        SearchParams(query="向量数据库", lang="en"),
        SearchParams(query="向量数据库", extract=False),
        SearchParams(query="向量数据库", stages=("understand", "recall")),
    ]
    keys = {query_key(v) for v in variants}
    assert base not in keys
    assert len(keys) == len(variants)  # 五个变体两两不同


def test_url_key_is_case_and_space_insensitive() -> None:
    assert url_key("HTTPS://Example.COM/a") == url_key("https://example.com/a ")


# ---------------------------------------------------------------- query_ttl
def test_ttl_fresh_beats_evergreen() -> None:
    """「最新 API 文档」同时命中时效与常青 → 走短档。拿一周前的结果答「最新」是说谎。"""
    p = SearchParams(query="最新的 API 文档", freshness="auto")
    assert query_ttl(p, _CFG, fresh=True, evergreen=True) == _CFG.ttl_query_fresh


def test_ttl_evergreen_over_default() -> None:
    """常青档不要求 freshness=any：auto 是缺省值，若要求 any 这一档几乎永不命中。"""
    p = SearchParams(query="PostgreSQL 官方文档", freshness="auto")
    assert query_ttl(p, _CFG, fresh=False, evergreen=True) == _CFG.ttl_query_evergreen


def test_ttl_explicit_day_week_forces_fresh() -> None:
    """调用方显式说了 day/week，即使自动检测没识别出时效词也走短档。"""
    p = SearchParams(query="向量数据库", freshness="week")
    assert query_ttl(p, _CFG, fresh=False, evergreen=False) == _CFG.ttl_query_fresh


def test_ttl_default_bucket() -> None:
    p = SearchParams(query="上海迪士尼门票", freshness="any")
    assert query_ttl(p, _CFG, fresh=False, evergreen=False) == _CFG.ttl_query_default


def test_ttl_ordering_is_fresh_shortest_evergreen_longest() -> None:
    """三档必须真的分得开，否则分档只是注释里的装饰。"""
    assert _CFG.ttl_query_fresh < _CFG.ttl_query_default < _CFG.ttl_query_evergreen


# ---------------------------------------------------------------- percentile
def test_percentile_hand_computed() -> None:
    """最近邻法：idx = round(pct/100 × (n-1))，n=5 → p50 取第 3 小、p95 取最大。"""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10
    assert percentile(values, 50) == 30
    assert percentile(values, 95) == 50
    assert percentile(values, 100) == 50


def test_percentile_sorts_input_and_handles_degenerate_cases() -> None:
    assert percentile([50.0, 10.0, 40.0, 20.0, 30.0], 50) == 30  # 乱序也要先排
    assert percentile([7.4], 95) == 7  # 单样本：p50/p95 都是它自己
    assert percentile([], 50) == 0  # 空样本不能抛 ZeroDivisionError


# ---------------------------------------------------------------- Store 往返与过期
def test_put_get_query_roundtrip(store: Store, frozen_now) -> None:
    payload = {"results": [{"url": "https://example.com/a"}], "flags": {"rerank": "ok"}}
    key = query_key(SearchParams(query="向量数据库"))
    store.put_query(key, payload, ttl=60)
    assert store.get_query(key) == payload  # 中文与嵌套结构都要原样回来


def test_get_query_miss_returns_none_and_counts(store: Store) -> None:
    assert store.get_query("nonexistent") is None
    assert store.get_value(MISS_QUERY) == 1


def test_expiry_boundary_is_exclusive(store: Store, frozen_now) -> None:
    """expires_at == now 仍算有效（`<` 而非 `<=`），下一秒才失效。差 1 秒不至于出错，
    但边界写反会让所有短 TTL 缓存提前一秒集体失效。"""
    store.put_query("k", {"v": 1}, ttl=60)
    frozen_now(60)
    assert store.get_query("k") == {"v": 1}
    frozen_now(1)
    assert store.get_query("k") is None


def test_expired_row_is_deleted_not_just_hidden(store: Store, frozen_now) -> None:
    """过期行要真删掉：只判不删的话库体积会一路涨（长驻服务跑几周就很可观）。"""
    store.put_query("k1", {"v": 1}, ttl=10)
    store.put_url("u1", {"v": 1}, ttl=10)
    frozen_now(11)
    assert store.purge_expired() == 2
    assert store.snapshot()["rows"] == {"query_cache": 0, "url_cache": 0}


def test_ttl_zero_still_writes_a_row(store: Store) -> None:
    """ttl 被 max(1, ttl) 兜底：ttl=0 不该让写入变成 no-op（否则「关缓存」与「写失败」分不清）。"""
    store.put_query("k", {"v": 1}, ttl=0)
    assert store.snapshot()["rows"]["query_cache"] == 1


def test_put_overwrites_and_extends_expiry(store: Store, frozen_now) -> None:
    store.put_query("k", {"v": 1}, ttl=10)
    frozen_now(5)
    store.put_query("k", {"v": 2}, ttl=10)
    frozen_now(9)  # 距首次写入 14s（早已过第一个 TTL），距第二次 4s
    assert store.get_query("k") == {"v": 2}


# ---------------------------------------------------------------- 指标
def test_hit_rate_reflects_hits_and_misses(store: Store, frozen_now) -> None:
    store.put_query("k", {"v": 1}, ttl=600)
    store.get_query("miss-1")
    store.get_query("k")
    snap = store.snapshot()
    assert snap["counters"][HIT_QUERY] == 1
    assert snap["counters"][MISS_QUERY] == 1
    assert snap["hit_rate"]["query"] == 0.5


def test_hit_rate_zero_when_no_traffic(store: Store) -> None:
    """没有流量时命中率是 0.0 而不是 ZeroDivisionError——/metrics 要能在冷启动时被调。"""
    assert store.snapshot()["hit_rate"] == {"query": 0.0, "url": 0.0}


def test_add_latency_is_a_ring_buffer(store: Store) -> None:
    """样本必须封顶：不封顶则延迟指标会被几个月前的数据稀释，看不出最近变慢了。"""
    for ms in (100, 200, 300, 400, 500):
        store.add_latency("pipeline", ms, limit=3)
    lat = store.snapshot()["latency_ms"]["pipeline"]
    assert lat["count"] == 3
    assert lat["avg"] == 400  # 只留最后三个 (300,400,500)
    assert lat["p50"] == 400


def test_snapshot_separates_counters_from_extra(store: Store) -> None:
    """非数值指标（引擎清单）不能被塞进 counters，否则 /metrics 的消费方要做类型防御。"""
    store.bump("search_total", 3)
    store.set_value(LAST_UNRESPONSIVE, ["google", "brave"])
    snap = store.snapshot()
    assert snap["counters"]["search_total"] == 3
    assert snap["extra"][LAST_UNRESPONSIVE] == ["google", "brave"]
    assert LAST_UNRESPONSIVE not in snap["counters"]


def test_bump_accumulates_across_calls(store: Store) -> None:
    store.bump("search_total")
    store.bump("search_total", 4)
    assert store.get_value("search_total") == 5


def test_reset_clears_everything(store: Store, frozen_now) -> None:
    """评测跑基线前必须能洗库：留着上一轮的缓存会把命中率与延迟都洗成虚高。"""
    store.put_query("k", {"v": 1}, ttl=600)
    store.put_url("u", {"v": 1}, ttl=600)
    store.bump("search_total")
    store.add_latency("pipeline", 120)
    store.reset()
    snap = store.snapshot()
    assert snap["counters"] == {}
    assert snap["rows"] == {"query_cache": 0, "url_cache": 0}
    assert snap["latency_ms"] == {}


def test_reset_can_narrow_to_query_cache_only(store: Store, frozen_now) -> None:
    """评测每轮只该清 query_cache：url_cache 存的是抽到的正文，与融合/精排的改动无关，
    一并清掉会让每轮重抽上百个页面，而指标并不会因此更真。"""
    store.put_query("k", {"v": 1}, ttl=600)
    store.put_url("u", {"v": 1}, ttl=600)
    store.bump("search_total")
    store.reset(("query_cache",))
    snap = store.snapshot()
    assert snap["rows"] == {"query_cache": 0, "url_cache": 1}
    assert store.get_url("u") == {"v": 1}
    assert store.get_query("k") is None
    assert snap["counters"]["search_total"] == 1


def test_reset_rejects_unknown_table(store: Store) -> None:
    """表名会拼进 SQL，白名单就是注入防线：宁可报错也不能把任意字符串送到 DELETE FROM 后面。"""
    with pytest.raises(ValueError, match="未知的缓存表"):
        store.reset(("query_cache; DROP TABLE metrics",))


def test_store_persists_across_reopen(tmp_path: Path) -> None:
    """跨重启保留是选 sqlite 而非内存字典的全部理由：容器重启后评测还能复跑。"""
    path = tmp_path / "websearch.db"
    first = Store(path)
    first.init()
    first.put_query("k", {"v": 1}, ttl=600)
    first.bump("search_total")
    first.close()

    second = Store(path)
    second.init()
    assert second.get_query("k") == {"v": 1}
    assert second.get_value("search_total") == 1
    second.close()


# ---------------------------------------------------------------- 异步门面
def test_async_store_roundtrip(tmp_path: Path) -> None:
    """异步门面走 to_thread：sqlite3 是同步阻塞库，直接在事件循环里调会卡住整条流水线。"""
    store = Store(tmp_path / "websearch.db")

    async def scenario() -> dict[str, object]:
        a = AsyncStore(store, _CFG)
        await a.init()
        await a.put_query("k", {"v": 1}, ttl=600)
        await a.put_url("u", {"v": 2}, ttl=600)
        await a.bump("search_total")
        await a.add_latency("pipeline", 1234)
        await a.set_value(LAST_UNRESPONSIVE, ["brave"])
        got = await a.get_query("k")
        snap = await a.snapshot()
        return {"got": got, "snap": snap, "unresponsive": await a.get_value(LAST_UNRESPONSIVE)}

    try:
        out = asyncio.run(scenario())
    finally:
        store.close()

    assert out["got"] == {"v": 1}
    assert out["unresponsive"] == ["brave"]
    snap = out["snap"]
    assert isinstance(snap, dict)
    assert snap["counters"]["search_total"] >= 1
    assert snap["latency_ms"]["pipeline"]["count"] == 1
    assert snap["rows"] == {"query_cache": 1, "url_cache": 1}


def test_async_init_purges_expired(tmp_path: Path, frozen_now) -> None:
    """init 顺带清一次过期行：服务长期不重启时，这是唯一会定期跑的清理时机。"""
    store = Store(tmp_path / "websearch.db")
    store.init()
    store.put_query("old", {"v": 1}, ttl=10)
    store.close()

    frozen_now(600)
    reopened = Store(tmp_path / "websearch.db")

    async def scenario() -> dict[str, int]:
        await AsyncStore(reopened, _CFG).init()
        snap = await AsyncStore(reopened, _CFG).snapshot()
        return snap["rows"]

    try:
        rows = asyncio.run(scenario())
    finally:
        reopened.close()
    assert rows == {"query_cache": 0, "url_cache": 0}


def test_roundtrip_under_real_clock(tmp_path: Path) -> None:
    """不 freeze 时钟也要能正常存取：上面的过期用例全靠 monkeypatch，这条兜住真实路径。"""
    store = Store(tmp_path / "websearch.db")
    try:
        store.init()
        store.put_query("k", {"v": "真实时钟"}, ttl=3600)
        assert store.get_query("k") == {"v": "真实时钟"}
        assert store.get_value(HIT_QUERY) == 1
        assert store.purge_expired() == 0  # 一小时内不该清掉任何东西
    finally:
        store.close()
