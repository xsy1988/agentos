"""mcp_client：MCP 连接池 + 健康检查 + 热注册（模块详细设计 §1.3.3）。

进程模型约束（设计方案 §7）：单进程 workers=1，池是进程内单例。

- 每 Server 一条会话，工具调用经 asyncio.Lock 串行排队
- 健康检查：每 60s list_tools，连续 3 次失败 → unhealthy + 落库 + **从池中摘除**
- 自愈：每轮探活末尾 rebuild() 补缺失连接 → Server（容器）重启后最多 60s 自动接回
- 热注册：监听 pg NOTIFY 'capability_changed' → 增量重建（M3-d）
- 工具级开关：list_tools 输出经 capability_tools.enabled 过滤（上下文膨胀最后闸门）
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select, text

from app.core.config import settings
from app.core.db import session_factory

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 60  # 秒（设计：每 60s 检查一次）
HEALTH_FAILURE_LIMIT = 3  # 连续失败次数 → unhealthy（60s×3 = 3 分钟内，DoD）


class McpNotConnected(RuntimeError):
    """会话未建立或 owner 任务已终结（死会话）。

    与「MCP 工具执行报错」（普通 RuntimeError）刻意区分：只有前者代表连接层坏了，
    池的 call_tool 命中它才触发即时 retire+rebuild+重试自愈。
    """


def mcp_connectable(payload: dict[str, Any]) -> bool:
    """payload 是否声明了可用的 MCP transport（stdio/http）。

    纯前端 plugin（只有 frontend iframe 清单、无 transport，如「采购决策面板」）不是
    MCP Server；连接池 rebuild 与注册冒烟都必须跳过它，否则每轮 rebuild 都会
    open_mcp_session → ValueError「未知 transport: None」刷屏并把它误判 unhealthy。
    """
    return isinstance(payload, dict) and payload.get("transport") in ("stdio", "http")


def _pg_dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def open_mcp_session(payload: dict[str, Any]) -> AsyncIterator[ClientSession]:
    """按 payload.transport 建立 MCP 会话（冒烟与连接池共用的原语）。

    secret_env 解密后合并进 env；stdio 拉起子进程，http 走 streamable http。
    """
    from app.modules.capabilities.service import decrypt_secret_env

    env = {**(payload.get("env") or {})}
    env.update(decrypt_secret_env(payload))
    transport = payload.get("transport")
    if transport == "stdio":
        params = StdioServerParameters(
            command=payload["command"],
            args=payload.get("args") or [],
            env=env or None,
        )
        # stderr 默认继承服务进程（nohup 重定向到日志），Server 报错可在日志直接看
        async with stdio_client(params) as (read, write):  # noqa: SIM117
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == "http":
        headers = payload.get("headers") or None
        async with (
            streamablehttp_client(payload["url"], headers=headers) as (
                read,
                write,
                _,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    else:
        raise ValueError(f"未知 transport: {transport}")


class McpConnection:
    """单 Server 连接：会话生命周期由专属 owner 任务独占持有（anyio cancel scope 不外泄）。

    永久修复 CancelledError 泄漏（容器重启实测）：streamablehttp/stdio 会话内部是 anyio
    task group + cancel scope，而 cancel scope 必须在**同一个任务**里 enter 和 exit。旧实现
    用 AsyncExitStack 在 rebuild 所在任务 enter 会话、却可能在健康循环/停机任务里 aclose，
    cancel scope 跨任务 → teardown 的 CancelledError 泄漏进健康循环的 sleep、uvicorn
    lifespan、甚至 SQLAlchemy 连接，把自愈循环打死、能力永久失联（只能重启后端恢复）。

    现在：一个 `_owner` 任务独占 `async with open_mcp_session(...)` 的 enter 与 exit，会话
    只在该任务内建立与拆除，并把所有取消吞在自己内部（见 `_run`）。`call_tool` /
    `health_probe` 仍从调用方任务用 session 收发（anyio 内存流支持跨任务），但**从不
    enter/exit 会话的 cancel scope**；`close()` 只负责请 owner 退出并回收——泄漏不再发生。
    """

    def __init__(self, cap_id: UUID, name: str, payload: dict[str, Any]) -> None:
        self.cap_id = cap_id
        self.name = name
        self.payload = payload
        self.lock = asyncio.Lock()  # 每会话串行（设计纪律）：call_tool / health_probe 互斥
        self.session: ClientSession | None = None  # 由 owner 任务建立；调用方只读取引用
        self.consecutive_failures = 0
        # 最近一次 list_tools 的工具签名缓存（OpenAI 函数签名数据源）
        self.tools_cache: dict[str, dict[str, Any]] = {}
        self._owner: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._connect_error: BaseException | None = None

    async def connect(self) -> None:
        """拉起 owner 任务并等待会话就绪；连接失败则回收 owner 并抛出（归一化为 Exception）。"""
        self._owner = asyncio.create_task(self._run(), name=f"mcp-conn-{self.name}")
        await self._ready.wait()
        if self._connect_error is not None:
            err = self._connect_error
            self._connect_error = None
            await self._reap_owner()
            # 归一化：任何 BaseException（含 anyio ExceptionGroup / 泄漏的 CancelledError）
            # 都不能穿透到 rebuild 的 `except Exception` 之上去打死健康循环
            if isinstance(err, Exception):
                raise err
            raise McpNotConnected(f"MCP 会话建立失败: {self.name}: {type(err).__name__}") from err
        if self.session is None:
            await self._reap_owner()
            raise McpNotConnected(f"MCP 会话未建立: {self.name}")
        self.consecutive_failures = 0

    async def _run(self) -> None:
        """owner 任务：独占 enter/exit 会话，把一切取消吞在内部，绝不外泄给调用方。"""
        try:
            async with open_mcp_session(self.payload) as session:
                self.session = session
                self._ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            # 两种来源：① close() 主动 cancel 本任务；② 对端把会话拖垮时 streamablehttp
            # 泄漏的 cancel scope。两者都必须在此终结——吞掉，让 owner 正常结束，绝不随
            # `await owner` 把 CancelledError 传回 close()/connect() 的调用方（那正是旧 bug）。
            pass
        except BaseException as e:  # noqa: BLE001
            # 建连/拆除的一切异常（含 anyio ExceptionGroup）都收敛在这一个任务里
            self._connect_error = e
        finally:
            self.session = None
            self._ready.set()  # 失败也要解除 connect() 的等待

    async def _reap_owner(self) -> None:
        """回收已结束的 owner 任务，避免「Task exception was never retrieved」告警。"""
        owner = self._owner
        self._owner = None
        if owner is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await owner

    def _alive(self) -> bool:
        """会话是否可用：owner 任务在跑且 session 已建立。"""
        return self.session is not None and self._owner is not None and not self._owner.done()

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """调用工具，返回文本化结果。串行排队。

        死会话（owner 已终结 / 对端拆流）抛 McpNotConnected 或 anyio ClosedResourceError，
        由池的 call_tool 自愈；工具本身报错抛 RuntimeError（不自愈）。
        """
        session = self.session
        if session is None or not self._alive():
            raise McpNotConnected(f"MCP Server 未连接: {self.name}")
        async with self.lock:
            result = await session.call_tool(tool_name, args)
        if result.isError:
            raise RuntimeError(f"MCP 工具执行报错: {self._content_text(result)}")
        self.consecutive_failures = 0
        return self._content_text(result)

    async def health_probe(self) -> list[Any]:
        """list_tools 探活：返回工具清单并刷新签名缓存（抛错 = 不健康，由 _health_round 计数）。"""
        session = self.session
        if session is None or not self._alive():
            raise McpNotConnected(f"MCP Server 未连接: {self.name}")
        async with self.lock:
            resp = await session.list_tools()
        self.consecutive_failures = 0
        tools = list(resp.tools)
        self.tools_cache = {
            t.name: {
                "description": t.description or "",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in tools
        }
        return tools

    async def close(self) -> None:
        """请 owner 任务退出并回收：teardown 的 cancel scope 全锁在 owner 内部，不外泄。"""
        self._stop.set()
        owner = self._owner
        self._owner = None
        if owner is not None and not owner.done():
            try:
                await asyncio.wait_for(owner, timeout=5.0)
            except TimeoutError:
                owner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await owner
            except asyncio.CancelledError:
                # close() 自身在停机时被取消：仍要回收 owner，再把取消如实上抛
                owner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await owner
                self._reset()
                raise
            except Exception:  # noqa: BLE001 —— 关闭失败的噪音不掩盖主流程
                logger.warning("mcp connection close failed: %s", self.name)
        self._reset()

    def _reset(self) -> None:
        self.session = None
        self.tools_cache = {}

    @staticmethod
    def _content_text(result: Any) -> str:
        parts = []
        for block in result.content or []:
            text_part = getattr(block, "text", None)
            if text_part:
                parts.append(str(text_part))
        return "\n".join(parts) or "(空结果)"


class McpPool:
    """进程内连接池单例。start() 装配 + 健康循环 + NOTIFY 监听。"""

    def __init__(self) -> None:
        self._conns: dict[UUID, McpConnection] = {}
        self._health_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        self._listen_conn: Any = None
        # 并发 rebuild（NOTIFY + 健康循环 + 引擎 call_tool 自愈同时触发）会给同一 cap_id
        # 建出两条连接、其中一条的 owner 任务沦为没人关的孤儿（会话泄漏）。一把锁串行。
        self._rebuild_lock = asyncio.Lock()
        # stop() 置位：用来区分「停机时对本任务的真正取消」与「对端拖垮会话时
        # streamablehttp 泄漏进来的 CancelledError」——只有前者该让健康循环退出
        self._stopping = False

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self.rebuild()
        self._health_task = asyncio.create_task(self._health_loop(), name="mcp-health")
        self._listener_task = asyncio.create_task(self._listen_changes(), name="mcp-changes")
        logger.info("McpPool started (%d servers)", len(self._conns))

    async def stop(self) -> None:
        self._stopping = True
        for t in (self._health_task, self._listener_task):
            if t:
                t.cancel()
        if self._listen_conn:
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
        for conn in list(self._conns.values()):
            await conn.close()
        self._conns.clear()
        logger.info("McpPool stopped")

    async def rebuild(self) -> None:
        """按注册表全量重建（启动时 / capability_changed 后 / call_tool 自愈时）。串行化。"""
        async with self._rebuild_lock:
            await self._rebuild_unlocked()

    async def _rebuild_unlocked(self) -> None:
        from app.modules.capabilities.models import Capability

        async with session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(Capability).where(
                            Capability.type.in_(("mcp", "plugin")),  # type: ignore[attr-defined]
                            Capability.enabled.is_(True),  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )
            targets = {r.id: (r.name, r.payload or {}) for r in rows}
            # 纯前端 plugin（无 transport，如「采购决策面板」iframe）不是可连接 MCP Server，
            # 跳过：否则每轮 rebuild 都 open_mcp_session → ValueError「未知 transport」刷屏
            targets = {cid: v for cid, v in targets.items() if mcp_connectable(v[1])}

        # 关掉已下线的
        for cap_id in list(self._conns):
            if cap_id not in targets:
                await self._conns.pop(cap_id).close()
        # 拉起新增/重建的（失败不阻断其他 Server）
        for cap_id, (name, payload) in targets.items():
            existing = self._conns.get(cap_id)
            if existing is not None:
                continue
            conn = McpConnection(cap_id, name, payload)
            try:
                await conn.connect()
                self._conns[cap_id] = conn
                await self._set_health(cap_id, "healthy")
                await self._sync_tools(cap_id, await conn.health_probe())
            except Exception as e:  # noqa: BLE001 —— 单 Server 失败不拖垮池
                logger.warning("mcp server connect failed: %s (%s)", name, e)
                await conn.close()
                await self._set_health(cap_id, "unhealthy")

    # ---------- 工具调用（engine tools 节点入口） ----------

    async def call_tool(self, cap_id: UUID, tool_name: str, args: dict[str, Any]) -> str:
        conn = self._conns.get(cap_id)
        if conn is None:
            # 不在池中（刚被摘除 / 启动时没连上）：即时 rebuild 一次再取
            await self._rebuild_guarded()
            conn = self._conns.get(cap_id)
            if conn is None:
                raise McpNotConnected(f"MCP Server 不在池中: {cap_id}")
        try:
            return await conn.call_tool(tool_name, args)
        except (McpNotConnected, ClosedResourceError, BrokenResourceError, EndOfStream) as e:
            # 死会话（对端容器/进程重启）：即时 retire + rebuild + 重试一次，不干等 60s
            # 健康循环——用户实测诉求：web_search 命中死会话应当立刻自愈而非整条流水线瘫掉
            logger.warning(
                "mcp call hit dead session %s (%s); retire+rebuild+retry once",
                conn.name,
                type(e).__name__,
            )
            await self._retire(cap_id, conn)
            await self._rebuild_guarded()
            fresh = self._conns.get(cap_id)
            if fresh is None or fresh is conn:
                raise
            return await fresh.call_tool(tool_name, args)

    async def _rebuild_guarded(self) -> None:
        """在任意任务（引擎请求 / 健康循环）里安全跑一次 rebuild。

        吞掉泄漏取消与 DB 抖动，绝不让调用方被带崩；唯有停机（_stopping）如实上抛取消。
        """
        try:
            await self.rebuild()
        except asyncio.CancelledError:
            if self._shutting_down():
                raise
            logger.warning("mcp rebuild cancelled during self-heal; ignored")
        except Exception as e:  # noqa: BLE001 —— DB 抖动不该终结调用方（引擎请求 / 健康循环）
            logger.warning("mcp rebuild failed during self-heal: %s", e)

    async def list_enabled_tools(self) -> list[dict[str, Any]]:
        """全部健康 Server 的启用工具（capability_tools 开关过滤后）。

        返回项：{capability_id, capability_name, tool_name, description, input_schema}
        —— schema 取自连接的 list_tools 缓存（探活时刷新），开关取自 DB。
        """
        from app.modules.capabilities.models import Capability, CapabilityTool

        out: list[dict[str, Any]] = []
        async with session_factory() as db:
            caps = (
                (
                    await db.execute(
                        select(Capability).where(
                            Capability.type.in_(("mcp", "plugin")),  # type: ignore[attr-defined]
                            Capability.enabled.is_(True),  # noqa: E712
                        )
                    )
                )
                .scalars()
                .all()
            )
            for cap in caps:
                conn = self._conns.get(cap.id)
                if conn is None:
                    continue
                tools = (
                    (
                        await db.execute(
                            select(CapabilityTool).where(CapabilityTool.capability_id == cap.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                for t in tools:
                    if not t.enabled:
                        continue
                    cached = conn.tools_cache.get(t.tool_name)
                    if cached is None:
                        continue  # Server 侧已下架
                    out.append(
                        {
                            "capability_id": cap.id,
                            "capability_name": cap.name,
                            "tool_name": t.tool_name,
                            "description": cached["description"],
                            "input_schema": cached["input_schema"],
                        }
                    )
        return out

    # ---------- 健康检查 ----------

    async def _health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                await self._health_round()
            except asyncio.CancelledError:
                if self._shutting_down():
                    raise
                # 兜底：owner-task 修复后取消已锁在各连接内部，理论上不再泄漏到这里；
                # 万一还有，循环也绝不能死（本次事故的直接死因就是无保护的 sleep）。
                # uncancel 清掉伪取消计数，否则下一轮 sleep 立刻再被取消 → 忙轮询空转。
                logger.warning("mcp health loop hit stray cancellation; surviving")
                with contextlib.suppress(RuntimeError):
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
            except Exception:  # noqa: BLE001 —— 任何一轮崩了都不能终结自愈循环
                logger.exception("mcp health round crashed; loop survives")

    def _shutting_down(self) -> bool:
        """健康循环是否正被 stop() 主动停机。用于给 CancelledError 分类。

        只认 _stopping 这个显式信号：stop() 先置位再 cancel 本任务，而它是本任务
        唯一的取消者。**不能**用 `Task.cancelling()` 判——对端容器重启时 streamablehttp
        的 anyio cancel scope 也是通过 task.cancel() 取消宿主的，同样会把 cancelling()
        抬到 >0；用它会把「会话被拖垮」误判成「停机」，于是把那个本应吞掉的
        CancelledError 重新放回，健康循环照样被打死（实测踩过的坑）。
        """
        return self._stopping

    async def _health_round(self) -> None:
        """一轮探活 + 自愈重建。拆出方法是为了可单测：不碰 sleep 与 while True。

        CancelledError 纪律：会话被对端拖垮时 health_probe()/rebuild() 会泄漏
        CancelledError（BaseException），普通 `except Exception` 抓不住。一旦逃逸就
        终结健康循环 → 容器恢复后该能力永久失联（DoD 8 自愈要防的正是这个）。
        故这里显式接住当作一次失败；唯有本任务被真正取消（_shutting_down）才上抛。
        """
        for cap_id, conn in list(self._conns.items()):
            try:
                await conn.health_probe()
                if conn.consecutive_failures == 0:
                    await self._set_health(cap_id, "healthy")
            except asyncio.CancelledError:
                if self._shutting_down():
                    raise
                conn.consecutive_failures += 1
                logger.warning(
                    "mcp probe cancelled by torn-down session: %s (%d)",
                    conn.name,
                    conn.consecutive_failures,
                )
                if conn.consecutive_failures >= HEALTH_FAILURE_LIMIT:
                    await self._retire(cap_id, conn)
            except Exception:  # noqa: BLE001 —— 探活失败计数
                conn.consecutive_failures += 1
                if conn.consecutive_failures >= HEALTH_FAILURE_LIMIT:
                    await self._retire(cap_id, conn)
        # 自愈：把上面摘掉的、以及启动时连不上但现在活过来的 Server 重新拉起。
        # rebuild() 幂等且单 Server 失败不拖垮池，所以每轮跑一次是安全的；
        # 容器恢复后最多 60s（一个探活周期）自动接回，无需重启后端
        try:
            await self.rebuild()
        except asyncio.CancelledError:
            if self._shutting_down():
                raise
            logger.warning("mcp pool rebuild cancelled; retry next round")
        except Exception as e:  # noqa: BLE001 —— DB 抖动不该终结整个健康循环
            logger.warning("mcp pool rebuild failed: %s", e)

    async def _retire(self, cap_id: UUID, conn: McpConnection) -> None:
        """判死一个 Server：落 unhealthy + 关闭 + 从池摘除，让 rebuild() 能重新拉起。

        必须从池里摘掉：rebuild() 对已存在的 cap_id 是 `continue`，留着一条死连接
        = 该能力永久失联（call_tool 抛「未连接」、list_enabled_tools 静默摘除其工具），
        直到后端进程重启。close() 对断裂会话可能再泄漏 CancelledError，已在 close 内接住。
        """
        logger.warning(
            "mcp server unhealthy: %s (%d consecutive failures)",
            conn.name,
            conn.consecutive_failures,
        )
        await self._set_health(cap_id, "unhealthy")
        await conn.close()
        self._conns.pop(cap_id, None)

    async def _listen_changes(self) -> None:
        """热注册：NOTIFY 'capability_changed' → 增量重建（不重启进程）。"""
        import asyncpg

        conn = await asyncpg.connect(_pg_dsn())
        self._listen_conn = conn

        def _on_notify(_c: Any, _p: Any, _ch: Any, payload: str) -> None:
            logger.info("capability_changed: %s → rebuild mcp pool", payload)
            asyncio.get_running_loop().create_task(self.rebuild())

        await conn.add_listener("capability_changed", _on_notify)
        try:
            await asyncio.Event().wait()
        except (asyncpg.InterfaceError, OSError):
            logger.info("capability_changed listener closed")

    # ---------- DB 落库 ----------

    async def _set_health(self, cap_id: UUID, status: str) -> None:

        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE capabilities SET health_status = :s, "
                    "last_health_check_at = now() WHERE id = :id"
                ),
                {"s": status, "id": cap_id},
            )
            await db.commit()

    async def _sync_tools(self, cap_id: UUID, tools: list[Any]) -> None:
        """list_tools 结果同步进 capability_tools（新工具默认 enabled）。"""
        from app.modules.capabilities.models import CapabilityTool

        async with session_factory() as db:
            existing = {
                t.tool_name: t
                for t in (
                    await db.execute(
                        select(CapabilityTool).where(CapabilityTool.capability_id == cap_id)
                    )
                )
                .scalars()
                .all()
            }
            changed = False
            for tool in tools:
                if tool.name in existing:
                    if existing[tool.name].description != (tool.description or ""):
                        existing[tool.name].description = tool.description or ""
                        changed = True
                    continue
                db.add(
                    CapabilityTool(
                        capability_id=cap_id,
                        tool_name=tool.name,
                        description=tool.description or "",
                        enabled=True,
                    )
                )
                changed = True
            # Server 侧已下架的工具保留记录（开关状态珍贵）但不再输出
            if changed:
                await db.commit()


# 单例（main.py lifespan 挂 start/stop）
mcp_pool = McpPool()
