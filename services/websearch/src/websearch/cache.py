"""两层缓存 + 指标累计（stdlib sqlite3，零依赖）。

为什么用 sqlite 而不是内存字典：跨重启保留（模型/容器重启后缓存还在，评测可复跑）、
可直接 `sqlite3 data/websearch.db 'select ...'` 排障、无额外服务。

三张表：
    query_cache  整条流水线短路（key = 归一化 query + 影响结果的参数）
    url_cache    抽取层短路（key = 归一化 URL）
    metrics      计数与延迟样本（value 为 JSON，供 /metrics 与 search_meta 读同一份数据）

纪律：sqlite3 是同步阻塞库，所有调用一律经 `asyncio.to_thread` 走线程池
（与平台「核心进程禁同步阻塞」同一纪律）；连接按线程本地持有，开 WAL 提升并发读写。
"""

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from websearch.config import Settings
from websearch.types import SearchParams

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS url_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_expires ON query_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_url_expires ON url_cache(expires_at);
"""

# 可被 `reset()` 清空的表（白名单：reset 的表名会拼进 SQL，只允许这三个模块内常量）
_TABLES: tuple[str, ...] = ("query_cache", "url_cache", "metrics")

# 计数名（命中率与召回量分布都从这里读）
HIT_QUERY = "query_cache_hit"
MISS_QUERY = "query_cache_miss"
HIT_URL = "url_cache_hit"
MISS_URL = "url_cache_miss"
SEARCH_TOTAL = "search_total"
SEARCH_DEGRADED = "search_degraded"
LATENCY_PREFIX = "latency."  # latency.pipeline / latency.recall …（JSON 数组，环形缓冲）
RECALL_DIST = "recall_raw_total"
LAST_UNRESPONSIVE = "last_unresponsive"  # 最近一次的无响应引擎清单（search_meta 报）


def _now() -> int:
    return int(time.time())


def query_key(params: SearchParams) -> str:
    """影响结果的参数才进 key：max_chars/max_results 只影响渲染，不进缓存键。"""
    raw = json.dumps(
        {
            "q": " ".join(params.query.split()).lower(),
            "site": params.site.lower(),
            "lang": params.lang.lower(),
            "freshness": params.freshness,
            "extract": params.extract,
            "stages": list(params.stages),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()


def url_key(url: str) -> str:
    return hashlib.sha1(url.strip().lower().encode()).hexdigest()


def query_ttl(params: SearchParams, cfg: Settings, *, fresh: bool, evergreen: bool) -> int:
    """TTL 由时效意图决定：问「最新」的东西不该缓存一周，问文档规范的不必每小时重抓。

    优先级：时效 > 常青 > 缺省。「最新 API 文档」同时命中前两者时走短 TTL——
    宁可多抓一次，也不能拿一周前的结果回答「最新」。
    """
    if params.freshness in ("day", "week") or fresh:
        return cfg.ttl_query_fresh
    # 常青不要求 freshness=any：默认 freshness=auto 的技术文档查询同样稳定，
    # 否则 7d 这一档几乎永远不会被命中（auto 是缺省值）
    if evergreen:
        return cfg.ttl_query_evergreen
    return cfg.ttl_query_default


def percentile(values: Sequence[float], pct: float) -> int:
    """最近邻法分位数（样本量小，插值意义不大）。

    参数收 `Sequence` 而不是 `list`：延迟样本存的是 int，而 list 是**不变**的，
    写成 `list[float]` 会让每个传 `list[int]` 的调用方都报错（只能靠强转掩盖）。
    """
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return int(ordered[idx])


class Store:
    """线程安全的 sqlite 缓存/指标仓库（同步方法；异步封装见模块底部）。"""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()  # 写操作串行（避免 SQLITE_BUSY 抖动）

    # ---------- 连接 ----------
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def init(self) -> None:
        with self._lock:
            self._conn().executescript(_SCHEMA)
            self._conn().commit()

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def purge_expired(self) -> int:
        """清理过期行（顺带控制库体积）。返回删除行数。"""
        now = _now()
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM query_cache WHERE expires_at < ?", (now,))
            deleted = cur.rowcount
            cur = conn.execute("DELETE FROM url_cache WHERE expires_at < ?", (now,))
            deleted += cur.rowcount
            conn.commit()
        return max(0, deleted)

    # ---------- query_cache ----------
    def get_query(self, key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT payload, expires_at FROM query_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self.bump(MISS_QUERY)
            return None
        if int(row["expires_at"]) < _now():
            with self._lock:
                self._conn().execute("DELETE FROM query_cache WHERE key = ?", (key,))
                self._conn().commit()
            self.bump(MISS_QUERY)
            return None
        self.bump(HIT_QUERY)
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def put_query(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        now = _now()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO query_cache(key, payload, created_at, expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (key, json.dumps(payload, ensure_ascii=False), now, now + max(1, ttl)),
            )
            conn.commit()

    # ---------- url_cache ----------
    def get_url(self, key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT payload, expires_at FROM url_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self.bump(MISS_URL)
            return None
        if int(row["expires_at"]) < _now():
            with self._lock:
                self._conn().execute("DELETE FROM url_cache WHERE key = ?", (key,))
                self._conn().commit()
            self.bump(MISS_URL)
            return None
        self.bump(HIT_URL)
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    def put_url(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        now = _now()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO url_cache(key, payload, created_at, expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "created_at=excluded.created_at, expires_at=excluded.expires_at",
                (key, json.dumps(payload, ensure_ascii=False), now, now + max(1, ttl)),
            )
            conn.commit()

    # ---------- metrics ----------
    def _get_metric(self, name: str) -> Any:
        row = self._conn().execute("SELECT value FROM metrics WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def _set_metric(self, name: str, value: Any) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO metrics(name, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (name, json.dumps(value, ensure_ascii=False), _now()),
            )
            conn.commit()

    def bump(self, name: str, delta: int = 1) -> None:
        cur = self._get_metric(name)
        base = int(cur) if isinstance(cur, (int, float)) else 0
        self._set_metric(name, base + delta)

    def set_value(self, name: str, value: Any) -> None:
        """写任意 JSON 值（如 last_unresponsive 引擎清单）。"""
        self._set_metric(name, value)

    def get_value(self, name: str) -> Any:
        return self._get_metric(name)

    def add_latency(self, stage: str, ms: int, limit: int = 200) -> None:
        """延迟样本（环形缓冲）。limit 之外的旧样本直接丢弃。"""
        name = f"{LATENCY_PREFIX}{stage}"
        cur = self._get_metric(name)
        samples = [int(x) for x in cur] if isinstance(cur, list) else []
        samples.append(int(ms))
        self._set_metric(name, samples[-max(1, limit) :])

    def snapshot(self, limit: int = 200) -> dict[str, Any]:
        """/metrics 与 search_meta 共用的指标视图。"""
        conn = self._conn()
        counters: dict[str, int] = {}
        latency: dict[str, dict[str, int]] = {}
        extra: dict[str, Any] = {}
        for row in conn.execute("SELECT name, value FROM metrics"):
            try:
                val = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
            name = str(row["name"])
            if name.startswith(LATENCY_PREFIX) and isinstance(val, list):
                nums = [float(x) for x in val]
                latency[name[len(LATENCY_PREFIX) :]] = {
                    "count": len(nums),
                    "p50": percentile(nums, 50),
                    "p95": percentile(nums, 95),
                    "avg": int(sum(nums) / len(nums)) if nums else 0,
                }
            elif isinstance(val, (int, float)):
                counters[name] = int(val)
            elif isinstance(val, (str, list, dict)):
                extra[name] = val
        q_hit, q_miss = counters.get(HIT_QUERY, 0), counters.get(MISS_QUERY, 0)
        u_hit, u_miss = counters.get(HIT_URL, 0), counters.get(MISS_URL, 0)
        rows = conn.execute(
            "SELECT (SELECT COUNT(*) FROM query_cache) AS q, (SELECT COUNT(*) FROM url_cache) AS u"
        ).fetchone()
        return {
            "counters": counters,
            "hit_rate": {
                "query": round(q_hit / (q_hit + q_miss), 4) if q_hit + q_miss else 0.0,
                "url": round(u_hit / (u_hit + u_miss), 4) if u_hit + u_miss else 0.0,
            },
            "latency_ms": latency,
            "rows": {"query_cache": int(rows["q"]), "url_cache": int(rows["u"])},
            "latency_samples": limit,
            "extra": extra,
        }

    def reset(self, tables: Sequence[str] | None = None) -> None:
        """清空缓存（评测跑基线前用，避免缓存把指标洗成虚高）。默认三张表全清。

        `tables` 只接受白名单内的表名：它会被拼进 SQL，白名单校验就是注入防线。

        评测默认只清 `query_cache`：`url_cache` 存的是抽到的正文，与融合/精排的改动
        无关，清掉它会让每轮评测重抽一遍全网（上百次）而并不让指标更真；
        `metrics` 是服务自己的累计计数，清了就没了历史趋势。
        """
        wanted = tuple(tables) if tables else _TABLES
        for table in wanted:
            if table not in _TABLES:
                raise ValueError(f"未知的缓存表：{table}（可选 {_TABLES}）")
        with self._lock:
            conn = self._conn()
            for table in wanted:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 —— 表名已过白名单
            conn.commit()


class AsyncStore:
    """异步门面：同步 sqlite 调用一律丢进线程池（不在事件循环里阻塞）。"""

    def __init__(self, store: Store, cfg: Settings):
        self.store = store
        self.cfg = cfg

    async def init(self) -> None:
        await asyncio.to_thread(self.store.init)
        await asyncio.to_thread(self.store.purge_expired)

    async def get_query(self, key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.get_query, key)

    async def put_query(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        await asyncio.to_thread(self.store.put_query, key, payload, ttl)

    async def get_url(self, key: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.get_url, key)

    async def put_url(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        await asyncio.to_thread(self.store.put_url, key, payload, ttl)

    async def bump(self, name: str, delta: int = 1) -> None:
        await asyncio.to_thread(self.store.bump, name, delta)

    async def set_value(self, name: str, value: Any) -> None:
        await asyncio.to_thread(self.store.set_value, name, value)

    async def get_value(self, name: str) -> Any:
        return await asyncio.to_thread(self.store.get_value, name)

    async def add_latency(self, stage: str, ms: int) -> None:
        await asyncio.to_thread(self.store.add_latency, stage, ms, self.cfg.latency_samples)

    async def snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self.store.snapshot, self.cfg.latency_samples)
