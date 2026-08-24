"""mcp_client：MCP 连接池 + 健康检查 + 热注册（模块详细设计 §1.3.3）。

进程模型约束（设计方案 §7）：单进程 workers=1，池是进程内单例。

- 每 Server 一条会话，工具调用经 asyncio.Lock 串行排队
- 健康检查：每 60s list_tools，连续 3 次失败 → unhealthy + 落库 + 摘除检索池
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import select, text

from app.core.config import settings
from app.core.db import session_factory

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 60  # 秒（设计：每 60s 检查一次）
HEALTH_FAILURE_LIMIT = 3  # 连续失败次数 → unhealthy（60s×3 = 3 分钟内，DoD）


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
    """单 Server 连接：会话 + 串行锁 + 健康账本。生命周期由 AsyncExitStack 持有。"""

    def __init__(self, cap_id: UUID, name: str, payload: dict[str, Any]) -> None:
        self.cap_id = cap_id
        self.name = name
        self.payload = payload
        self.lock = asyncio.Lock()  # 每会话串行（设计纪律）
        self.session: ClientSession | None = None
        self._stack: Any = None
        self.consecutive_failures = 0
        # 最近一次 list_tools 的工具签名缓存（OpenAI 函数签名数据源）
        self.tools_cache: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        # open_mcp_session 整体进入栈：两层上下文一起保持打开
        cm = open_mcp_session(self.payload)
        self.session = await self._stack.enter_async_context(cm)
        self.consecutive_failures = 0

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 —— 关闭失败的噪音不掩盖主流程
                logger.warning("mcp connection close failed: %s", self.name)
        self.session = None
        self._stack = None
        self.tools_cache = {}

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """调用工具，返回文本化结果。串行排队，异常上抛由 engine 兜底。"""
        if self.session is None:
            raise RuntimeError(f"MCP Server 未连接: {self.name}")
        async with self.lock:
            result = await self.session.call_tool(tool_name, args)
        if result.isError:
            raise RuntimeError(f"MCP 工具执行报错: {self._content_text(result)}")
        self.consecutive_failures = 0
        return self._content_text(result)

    async def health_probe(self) -> list[Any]:
        """list_tools 探活：返回工具清单并刷新签名缓存（抛错 = 不健康）。"""
        if self.session is None:
            raise RuntimeError(f"MCP Server 未连接: {self.name}")
        async with self.lock:
            resp = await self.session.list_tools()
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

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self.rebuild()
        self._health_task = asyncio.create_task(self._health_loop(), name="mcp-health")
        self._listener_task = asyncio.create_task(self._listen_changes(), name="mcp-changes")
        logger.info("McpPool started (%d servers)", len(self._conns))

    async def stop(self) -> None:
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
        """按注册表全量重建（启动时 / capability_changed 后）。"""
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
            raise RuntimeError(f"MCP Server 不在池中: {cap_id}")
        return await conn.call_tool(tool_name, args)

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
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            for cap_id, conn in list(self._conns.items()):
                try:
                    await conn.health_probe()
                    if conn.consecutive_failures == 0:
                        await self._set_health(cap_id, "healthy")
                except Exception:  # noqa: BLE001 —— 探活失败计数
                    conn.consecutive_failures += 1
                    if conn.consecutive_failures >= HEALTH_FAILURE_LIMIT:
                        logger.warning(
                            "mcp server unhealthy: %s (%d consecutive failures)",
                            conn.name,
                            conn.consecutive_failures,
                        )
                        await self._set_health(cap_id, "unhealthy")
                        await conn.close()
                        # 摘除检索池：关连接即可，rebuild 时若仍 enabled 会重试拉起

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
