"""EngineRuntime —— 引擎运行时（M2-2b）。

装配并持有：checkpointer(AsyncPostgresSaver) + 最小图 + inbox worker + LISTEN 监听。
进程模型：单进程 uvicorn workers=1（设计方案 §7 四纪律之一：引擎只在本进程跑一份）。

队列语义（模块详细设计 §1.1.3）：
- API 侧只投 inbox_events + NOTIFY，不触碰图
- worker 认领（FOR UPDATE SKIP LOCKED）→ user_input 起任务 / abort 取消任务
- 30s 轮询兜底防丢通知
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_factory
from app.modules.engine.graph import build_graph
from app.modules.runs.models import Run

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = ("done", "failed", "cancelled")


def _pg_dsn() -> str:
    """postgresql+asyncpg://… → postgresql://…（psycopg/asyncpg 原生连接用）。"""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


class EngineRuntime:
    def __init__(self) -> None:
        self.saver: AsyncPostgresSaver | None = None
        self._saver_cm: Any = None
        self.graph: CompiledStateGraph | None = None
        self._worker_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self._wakeup = asyncio.Event()
        self._run_tasks: dict[str, asyncio.Task] = {}
        # backend 协议的一期进程内实现
        from app.modules.engine.backend_impl import InProcessBackend

        self.backend = InProcessBackend()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._saver_cm = AsyncPostgresSaver.from_conn_string(_pg_dsn())
        self.saver = await self._saver_cm.__aenter__()
        await self.saver.setup()
        self.graph = build_graph(self)
        self._listener_task = asyncio.create_task(self._listen_inbox(), name="engine-listen")
        self._worker_task = asyncio.create_task(self._worker(), name="engine-worker")
        logger.info("EngineRuntime started")

    async def stop(self) -> None:
        for task in self._run_tasks.values():
            task.cancel()
        for t in (self._worker_task, self._listener_task):
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        if self._listen_conn:
            await self._listen_conn.close()
        if self._saver_cm:
            await self._saver_cm.__aexit__(None, None, None)
        logger.info("EngineRuntime stopped")

    # ---------- 事件生产（backend.emit_event 的本地直连版） ----------

    async def emit_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """写 run_events（seq = run 内 max+1）+ NOTIFY。SSE 的实时通道。"""
        async with session_factory() as db:
            result = await db.execute(
                text(
                    "INSERT INTO run_events (run_id, seq, event_type, payload) "
                    "SELECT :rid, COALESCE(MAX(seq), 0) + 1, :et, CAST(:p AS jsonb) "
                    "FROM run_events WHERE run_id = :rid RETURNING seq"
                ),
                {"rid": run_id, "et": event_type, "p": json.dumps(payload, ensure_ascii=False)},
            )
            seq = int(result.scalar_one())
            await db.execute(text("SELECT pg_notify('run_events', :rid)"), {"rid": run_id})
            await db.commit()
            return seq

    async def persist_assistant_message(
        self, conversation_id: str, run_id: str, text: str
    ) -> None:
        from app.modules.conversations.models import Conversation, Message

        async with session_factory() as db:
            db.add(
                Message(
                    conversation_id=UUID(conversation_id),
                    run_id=UUID(run_id),
                    role="assistant",
                    content={"text": text},
                )
            )
            conv = await db.get(Conversation, UUID(conversation_id))
            if conv:
                conv.message_count = (conv.message_count or 0) + 1
                conv.last_message_at = datetime.now(UTC)
            await db.commit()

    # ---------- inbox：监听与消费 ----------

    async def _listen_inbox(self) -> None:
        conn = await asyncpg.connect(_pg_dsn())
        self._listen_conn = conn

        def _on_notify(_conn: Any, _pid: Any, _channel: Any, payload: str) -> None:
            self._wakeup.set()

        await conn.add_listener("inbox_events", _on_notify)
        try:
            await asyncio.Event().wait()  # 挂住直到 stop() 关连接
        except (asyncpg.InterfaceError, OSError):
            logger.info("inbox listener closed")

    async def _claim_next(self) -> dict[str, Any] | None:
        async with session_factory() as db:
            result = await db.execute(
                text(
                    "UPDATE inbox_events SET status = 'consumed', consumed_at = now() "
                    "WHERE id = (SELECT id FROM inbox_events WHERE status = 'new' "
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    "RETURNING id, event_type, target_run_id, payload"
                )
            )
            row = result.mappings().first()
            if row is None:
                return None
            # 认领必须提交，否则 session 退出时 ROLLBACK，事件回到 new，
            # worker 会无限重复认领同一条事件（曾导致忙循环 + run 永远 pending）
            await db.commit()
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            return {
                "id": row["id"],
                "event_type": row["event_type"],
                "target_run_id": str(row["target_run_id"]) if row["target_run_id"] else None,
                "payload": payload or {},
            }

    async def _worker(self) -> None:
        while True:
            try:
                event = await self._claim_next()
                if event is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._wakeup.wait(), timeout=30)
                    self._wakeup.clear()
                    continue
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— worker 永不退出
                logger.exception("inbox worker iteration failed")

    async def _handle(self, event: dict[str, Any]) -> None:
        et = event["event_type"]
        if et == "user_input":
            run_id = event["payload"].get("run_id")
            if run_id:
                task = asyncio.create_task(self._process_run(str(run_id)))
                self._run_tasks[str(run_id)] = task
                task.add_done_callback(lambda t: self._run_tasks.pop(str(run_id), None))
        elif et == "abort":
            run_id = event.get("target_run_id")
            if run_id:
                running = self._run_tasks.get(run_id)
                if running:
                    running.cancel()

    # ---------- run 执行 ----------

    async def _process_run(self, run_id: str) -> None:
        run_uuid = UUID(run_id)
        final_text = ""
        try:
            async with session_factory() as db:
                run = await db.get(Run, run_uuid)
                if run is None or run.status != "pending":
                    return
                agent_id = str(run.agent_id)
                thread_id = str(run.conversation_id) if run.conversation_id else run_id
                input_text = (run.input or {}).get("text", "")
                timeout = run.budget.get("timeout_seconds") or 600

            await self._set_run_status(run_id, "running")
            await self.emit_event(run_id, "run_status", {"status": "running"})

            assert self.graph is not None
            final_state = await asyncio.wait_for(
                self.graph.ainvoke(
                    {"messages": [HumanMessage(content=input_text)]},
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "run_id": run_id,
                            "agent_id": agent_id,
                        }
                    },
                ),
                timeout=timeout,
            )
            msgs = final_state.get("messages") or []
            final_text = str(getattr(msgs[-1], "content", "")) if msgs else ""
            budget_used = dict(final_state.get("budget_state") or {})

            async with session_factory() as db:
                run = await db.get(Run, run_uuid)
                if run:
                    run.status = "done"
                    run.result = {"text": final_text}
                    run.budget_used = budget_used
                    run.finished_at = datetime.now(UTC)
                    await db.commit()
            await self.emit_event(run_id, "run_status", {"status": "done"})
        except asyncio.CancelledError:
            await self._set_run_status(run_id, "cancelled", error={"code": "aborted"})
            await self.emit_event(run_id, "run_status", {"status": "cancelled"})
        except TimeoutError:
            await self._set_run_status(
                run_id, "failed", error={"code": "timeout", "detail": "全局超时"}
            )
            await self.emit_event(
                run_id, "error", {"code": "timeout", "detail": "全局超时"}
            )
            await self.emit_event(run_id, "run_status", {"status": "failed"})
        except Exception as e:  # noqa: BLE001 —— 结构化错误统一落库
            logger.exception("run %s failed", run_id)
            await self._set_run_status(
                run_id, "failed", error={"code": type(e).__name__, "detail": str(e)[:500]}
            )
            await self.emit_event(
                run_id, "error", {"code": type(e).__name__, "detail": str(e)[:500]}
            )
            await self.emit_event(run_id, "run_status", {"status": "failed"})

    async def _set_run_status(
        self, run_id: str, status: str, error: dict | None = None
    ) -> None:
        async with session_factory() as db:
            run = await db.get(Run, UUID(run_id))
            if run:
                run.status = status
                if error:
                    run.error = error
                if status in TERMINAL_RUN_STATUSES:
                    run.finished_at = datetime.now(UTC)
                await db.commit()


# 单例（main.py lifespan 中 start/stop）
engine_runtime = EngineRuntime()
