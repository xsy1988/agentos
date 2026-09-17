"""MCP 连接池自愈单测（不碰 DB、不碰网络、不碰真实 MCP 会话）。

回归的缺陷：`_health_round` 判死一个 Server 后只 `close()`，**没有从 `_conns` 摘除**；
而 `rebuild()` 对已存在的 cap_id 直接 `continue` → 容器重启后该能力永久失联
（`call_tool` 抛「MCP Server 未连接」、`list_enabled_tools` 静默摘掉它的工具），
直到后端进程重启才恢复。自建网页搜索服务是容器栈，这种失联会天天发生。

修复后的契约（本文件逐条断言）：
连续失败 3 次 → 关连接 + 置 unhealthy + **从池中摘除** → 下一轮 rebuild 重新连上并置 healthy。

第二个回归（容器重启实测揪出）：MCP 会话被对端拖垮时，streamablehttp 内部的 anyio
cancel scope 会把 `CancelledError` 泄漏进 `health_probe()`/`close()`。它是 BaseException，
旧代码 `except Exception` 抓不住 → 健康循环当场暴毙 → 容器恢复后 websearch 永久失联，
只能重启后端。修复后：非停机时把它当一次探活失败，循环必须活着（见下方 cancel 组用例）。

第三个回归（上一版仍不彻底：生产 13.5h 失联实测揪出）：cancel scope 在 rebuild 所在任务
enter、却可能在健康循环/停机任务 exit（AsyncExitStack 跨任务 aclose）→ 泄漏的 CancelledError
打穿 `_health_loop` 里无保护的 `await asyncio.sleep()`，还窜进 uvicorn lifespan 与 SQLAlchemy。
永久修复：每条会话的 enter/use/exit 全锁进一个专属 owner 任务（`McpConnection._run`），anyio
取消只在该任务内生灭、绝不外泄；`_health_loop` 再加兜底（stray cancel 不许打死循环）；
`call_tool` 命中死会话即时 retire+rebuild+重试；池跳过无 transport 的纯前端 plugin。
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from anyio import ClosedResourceError

from app.modules.capabilities import mcp_client
from app.modules.capabilities.mcp_client import (
    HEALTH_FAILURE_LIMIT,
    McpConnection,
    McpPool,
    mcp_connectable,
)


class FakeConn:
    """假连接：health_probe 按 fail 开关成功/失败，close 只记账。

    只实现 `_health_round` 真正会碰的四个成员（health_probe / consecutive_failures /
    name / close），其余保持缺省——测试要盯的是池的簿记，不是连接内部。
    """

    def __init__(self, cap_id: UUID, name: str, *, fail: bool = True, cancel: bool = False) -> None:
        self.cap_id = cap_id
        self.name = name
        self.fail = fail
        self.cancel = cancel
        self.consecutive_failures = 0
        self.closed = False
        self.probe_count = 0

    async def health_probe(self) -> list[Any]:
        self.probe_count += 1
        if self.cancel:
            # 真实场景：容器重启把会话拖垮，streamablehttp 泄漏 CancelledError
            raise asyncio.CancelledError("session torn down")
        if self.fail:
            raise ConnectionError("server gone")
        self.consecutive_failures = 0
        return []

    async def close(self) -> None:
        self.closed = True


def _pool(
    conn: FakeConn | None,
    *,
    health_log: list[tuple[UUID, str]],
    rebuild: Callable[[], Awaitable[None]],
) -> McpPool:
    """造一个不落库的池：_set_health 只记账，rebuild 换成测试脚本。"""
    pool = McpPool()
    if conn is not None:
        pool._conns[conn.cap_id] = conn

    async def _set_health(cap_id: UUID, status: str) -> None:
        health_log.append((cap_id, status))

    pool._set_health = _set_health  # type: ignore[method-assign]
    pool.rebuild = rebuild  # type: ignore[method-assign]
    return pool


def _rounds(pool: McpPool, n: int) -> None:
    async def _run() -> None:
        for _ in range(n):
            await pool._health_round()

    asyncio.run(_run())


def test_dead_conn_is_removed_from_pool() -> None:
    """连续失败到上限 → 关连接 + unhealthy + 从 `_conns` 摘除（修复点）。"""
    cap_id = uuid4()
    dead = FakeConn(cap_id, "websearch")
    health: list[tuple[UUID, str]] = []
    rebuilt: list[int] = []

    async def _rebuild() -> None:
        rebuilt.append(1)

    pool = _pool(dead, health_log=health, rebuild=_rebuild)
    _rounds(pool, HEALTH_FAILURE_LIMIT)

    assert dead.closed is True
    assert cap_id not in pool._conns
    assert dead.consecutive_failures == HEALTH_FAILURE_LIMIT
    assert health[-1] == (cap_id, "unhealthy")
    # 前两轮不该误杀：只累计失败，不关连接
    assert health.count((cap_id, "unhealthy")) == 1


def test_rebuild_reconnects_after_container_recovers() -> None:
    """摘除后容器活过来：下一轮 rebuild 重新连上并置 healthy，探活恢复正常。"""
    cap_id = uuid4()
    dead = FakeConn(cap_id, "websearch")
    fresh = FakeConn(cap_id, "websearch", fail=False)
    health: list[tuple[UUID, str]] = []

    async def _rebuild() -> None:
        # 模拟 rebuild 的真实语义：池里没有这个 cap_id 才重连（已存在则 continue）
        if cap_id not in pool._conns:
            pool._conns[cap_id] = fresh
            await pool._set_health(cap_id, "healthy")

    pool = _pool(dead, health_log=health, rebuild=_rebuild)
    _rounds(pool, HEALTH_FAILURE_LIMIT)
    assert pool._conns[cap_id] is fresh

    # 恢复后再跑一轮：新连接探活成功，状态稳定在 healthy
    _rounds(pool, 1)
    assert pool._conns[cap_id] is fresh
    assert fresh.probe_count == 1
    assert not fresh.closed
    assert health[-1] == (cap_id, "healthy")


def test_rebuild_runs_every_round_even_when_all_healthy() -> None:
    """自愈心跳：全绿时也要每轮 rebuild，才能接住「启动时连不上、后来才起来」的 Server。"""
    cap_id = uuid4()
    ok = FakeConn(cap_id, "websearch", fail=False)
    health: list[tuple[UUID, str]] = []
    rebuilt: list[int] = []

    async def _rebuild() -> None:
        rebuilt.append(1)

    pool = _pool(ok, health_log=health, rebuild=_rebuild)
    _rounds(pool, 3)

    assert len(rebuilt) == 3
    assert cap_id in pool._conns
    assert not ok.closed
    assert health == [(cap_id, "healthy")] * 3


def test_rebuild_failure_does_not_kill_health_loop() -> None:
    """rebuild 抛错（DB 抖动）只记日志，不让整轮探活崩掉——否则自愈能力一次性报废。"""
    cap_id = uuid4()
    ok = FakeConn(cap_id, "websearch", fail=False)
    health: list[tuple[UUID, str]] = []

    async def _boom() -> None:
        raise RuntimeError("db down")

    pool = _pool(ok, health_log=health, rebuild=_boom)
    _rounds(pool, 2)  # 不抛即通过

    assert cap_id in pool._conns
    assert health == [(cap_id, "healthy")] * 2


def test_one_bad_server_does_not_affect_others() -> None:
    """多 Server 时判死只影响自己那一条，健康的那条继续留在池里。"""
    bad_id, good_id = uuid4(), uuid4()
    bad = FakeConn(bad_id, "websearch")
    good = FakeConn(good_id, "other-mcp", fail=False)
    health: list[tuple[UUID, str]] = []

    async def _rebuild() -> None:
        return None

    pool = McpPool()
    pool._conns = {bad_id: bad, good_id: good}

    async def _set_health(cap_id: UUID, status: str) -> None:
        health.append((cap_id, status))

    pool._set_health = _set_health  # type: ignore[method-assign]
    pool.rebuild = _rebuild  # type: ignore[method-assign]

    async def _run() -> None:
        for _ in range(HEALTH_FAILURE_LIMIT):
            await pool._health_round()

    asyncio.run(_run())

    assert bad_id not in pool._conns
    assert pool._conns[good_id] is good
    assert (bad_id, "unhealthy") in health
    assert (good_id, "healthy") in health


# ---------------------------------------------------------------- CancelledError 组


def test_cancelled_probe_does_not_kill_health_loop() -> None:
    """会话被拖垮泄漏 CancelledError：循环存活，照常计数→摘除→同轮 rebuild 重连。

    这是容器重启实测揪出的真回归：旧代码只在 `except Exception` 里计数，
    CancelledError（BaseException）直接穿透 `_health_round` 打死 mcp-health 任务。
    """
    cap_id = uuid4()
    dead = FakeConn(cap_id, "websearch", cancel=True)
    fresh = FakeConn(cap_id, "websearch", fail=False)
    health: list[tuple[UUID, str]] = []

    async def _rebuild() -> None:
        if cap_id not in pool._conns:
            pool._conns[cap_id] = fresh
            await pool._set_health(cap_id, "healthy")

    pool = _pool(dead, health_log=health, rebuild=_rebuild)
    _rounds(pool, HEALTH_FAILURE_LIMIT)  # 不抛即证明循环没被 CancelledError 打死

    assert dead.consecutive_failures == HEALTH_FAILURE_LIMIT
    assert dead.closed is True
    assert (cap_id, "unhealthy") in health
    assert pool._conns[cap_id] is fresh  # 摘除后同一轮 rebuild 立刻重连
    assert health[-1] == (cap_id, "healthy")


def test_close_confines_owner_teardown_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """owner 任务把会话 teardown 泄漏的 CancelledError 吞在内部：close() 不外泄、状态复位。

    本次事故真根因回归：旧实现用 AsyncExitStack 在**别的任务**里 aclose，streamablehttp 的
    anyio cancel scope 跨任务 → CancelledError 泄漏打穿调用方（健康循环 sleep / lifespan /
    SQLAlchemy）。现在 enter/exit 全锁在 owner 任务内，取消不再逃出来。
    """

    @contextlib.asynccontextmanager
    async def _fake_open(payload: dict[str, Any]) -> AsyncIterator[Any]:
        yield object()
        # 对端把会话拖垮：teardown 泄漏 CancelledError（贴近真机 streamablehttp 行为）
        raise asyncio.CancelledError("session torn down")

    monkeypatch.setattr(mcp_client, "open_mcp_session", _fake_open)

    conn = McpConnection(uuid4(), "websearch", {"transport": "http", "url": "x"})
    conn.tools_cache = {"web_search": {}}

    async def _run() -> None:
        await conn.connect()
        assert conn.session is not None
        await conn.close()  # 不得抛 CancelledError（旧 bug 会在此泄漏打穿停机路径）

    asyncio.run(_run())

    assert conn.session is None
    assert conn.tools_cache == {}
    assert conn._owner is None


def test_stopping_reraises_cancelled_probe() -> None:
    """停机时（_stopping=True）CancelledError 必须上抛，保证健康循环能干净退出。"""
    cap_id = uuid4()
    conn = FakeConn(cap_id, "websearch", cancel=True)
    health: list[tuple[UUID, str]] = []

    async def _rebuild() -> None:
        return None

    pool = _pool(conn, health_log=health, rebuild=_rebuild)
    pool._stopping = True

    async def _run() -> None:
        await pool._health_round()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())


def test_shutting_down_ignores_task_cancelling_count() -> None:
    """_shutting_down 只认 _stopping，不能被 `Task.cancelling()` 带偏。

    真机回归：anyio 的 cancel scope 是通过 task.cancel() 递送 CancelledError 的，
    会把宿主任务的 cancelling() 抬到 >0。若用它判停机，「会话被拖垮」会被误判成
    「停机」→ 重新上抛 → 健康循环被打死（容器重启实测踩过）。本用例在
    cancelling()>0 且 _stopping=False 时钉住 _shutting_down() 必须为 False。
    """
    pool = McpPool()

    async def _run() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0)  # 让 CancelledError 在此递送
        # 此刻 cancelling() 已 >0（复现 anyio 抬计数），但未停机
        assert task.cancelling() > 0
        assert pool._shutting_down() is False
        pool._stopping = True
        assert pool._shutting_down() is True
        task.uncancel()  # 收尾平衡计数（生产里由 anyio scope 负责）

    asyncio.run(_run())


def test_call_tool_self_heals_on_dead_session() -> None:
    """call_tool 命中死会话（ClosedResourceError）→ retire + rebuild + 用新连接重试成功。

    用户实测诉求：容器重启后 web_search 命中池中死会话，应立刻自愈返回结果，
    而不是干等 60s 健康循环、期间整条检索流水线全部 ClosedResourceError。
    """
    cap_id = uuid4()

    class _DeadConn:
        name = "websearch"
        consecutive_failures = 0

        def __init__(self) -> None:
            self.closed = False

        async def call_tool(
            self, tool_name: str, args: dict[str, Any], *, meta: dict[str, Any] | None = None
        ) -> str:
            raise ClosedResourceError

        async def close(self) -> None:
            self.closed = True

    class _FreshConn:
        name = "websearch"
        consecutive_failures = 0

        async def call_tool(
            self, tool_name: str, args: dict[str, Any], *, meta: dict[str, Any] | None = None
        ) -> str:
            return "ok-result"

        async def close(self) -> None:
            return None

    dead, fresh = _DeadConn(), _FreshConn()
    health: list[tuple[UUID, str]] = []
    pool = McpPool()
    pool._conns[cap_id] = dead  # type: ignore[assignment]

    async def _set_health(cap_id: UUID, status: str) -> None:
        health.append((cap_id, status))

    async def _rebuild() -> None:
        pool._conns[cap_id] = fresh  # type: ignore[assignment]

    pool._set_health = _set_health  # type: ignore[method-assign]
    pool.rebuild = _rebuild  # type: ignore[method-assign]

    result = asyncio.run(pool.call_tool(cap_id, "web_search", {"q": "x"}))

    assert result == "ok-result"  # 重试成功
    assert dead.closed is True  # 死连接被 retire 关闭
    assert pool._conns[cap_id] is fresh  # 换成新连接
    assert (cap_id, "unhealthy") in health  # retire 落了 unhealthy


def test_health_loop_survives_stray_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底：即便 stray CancelledError 逃进 _health_loop，循环也不许死，且继续跑后续轮。

    直接死因回归：旧 `_health_loop` 的 `await asyncio.sleep()` 无保护，泄漏的取消在此
    终结整个 mcp-health 任务 → 不再 rebuild → 能力永久失联。
    """
    monkeypatch.setattr(mcp_client, "HEALTH_CHECK_INTERVAL", 0.01)
    pool = McpPool()
    rounds = {"n": 0}

    async def _round() -> None:
        rounds["n"] += 1
        if rounds["n"] == 1:
            # 忠实模拟 anyio scope 泄漏：它通过 task.cancel() 递送 CancelledError
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)  # 让 CancelledError 在此递打进 _health_loop

    pool._health_round = _round  # type: ignore[method-assign]

    async def _run() -> tuple[bool, int]:
        task = asyncio.create_task(pool._health_loop())
        await asyncio.sleep(0.08)  # 让它跑几轮
        alive, n = not task.done(), rounds["n"]
        pool._stopping = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return alive, n

    alive, n = asyncio.run(_run())

    assert alive is True  # 循环没被那次 stray cancel 打死
    assert n >= 2  # 且继续跑了后续轮


def test_mcp_connectable_skips_frontend_only_plugin() -> None:
    """纯前端 plugin（无 transport，如「采购决策面板」iframe）不可连接，池/冒烟须跳过。"""
    assert mcp_connectable({"frontend": {"mode": "iframe", "url": "x"}}) is False
    assert mcp_connectable({}) is False
    assert mcp_connectable({"transport": "http", "url": "x"}) is True
    assert mcp_connectable({"transport": "stdio", "command": "x"}) is True
    assert mcp_connectable({"transport": "grpc"}) is False
