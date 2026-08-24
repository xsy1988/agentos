"""EngineRuntime —— 引擎运行时（M2-2b/2c）。

装配并持有：checkpointer(AsyncPostgresSaver) + 完整图 + inbox worker + LISTEN 监听
+ 钩子链（audit → metering → budget → loop-detect）+ 每 run 执行上下文。
进程模型：单进程 uvicorn workers=1（设计方案 §7 四纪律之一：引擎只在本进程跑一份）。

队列语义（模块详细设计 §1.1.3）：
- API 侧只投 inbox_events + NOTIFY，不触碰图
- worker 认领（FOR UPDATE SKIP LOCKED）→ user_input 起任务 / abort 取消 / confirmation 恢复
- 30s 轮询兜底防丢通知

确认点（interrupt）语义：
- 图内 interrupt → ainvoke 返回 → run 置 paused_awaiting_confirm + confirmation_request 事件
- 用户确认（runs 控制面投 confirmation 事件）→ Command(resume=answer) 从检查点继续
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import asyncpg
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_factory
from app.modules.engine.graph import build_graph
from app.modules.engine.hooks import BudgetExceededError, RunContext
from app.modules.engine.state import BudgetState
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
        self.hooks: Any = None  # HookChain（audit → metering → budget → loop-detect）
        self._worker_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self._wakeup = asyncio.Event()
        self._run_tasks: dict[str, asyncio.Task] = {}
        self._run_ctx: dict[str, RunContext] = {}  # run_id → 执行上下文（钩子/节点共享账本）
        # backend 协议的一期进程内实现
        from app.modules.engine.backend_impl import InProcessBackend

        self.backend = InProcessBackend()

    def get_run_ctx(self, run_id: str) -> RunContext:
        if run_id not in self._run_ctx:
            self._run_ctx[run_id] = RunContext(run_id, None, "")
        return self._run_ctx[run_id]

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        from app.modules.engine.hooks_impl import build_default_chain

        self._saver_cm = AsyncPostgresSaver.from_conn_string(_pg_dsn())
        self.saver = await self._saver_cm.__aenter__()
        await self.saver.setup()
        self.hooks = build_default_chain(self.emit_event)
        self.graph = build_graph(self)
        await self._reconcile_orphans()
        self._listener_task = asyncio.create_task(self._listen_inbox(), name="engine-listen")
        self._worker_task = asyncio.create_task(self._worker(), name="engine-worker")
        logger.info("EngineRuntime started")

    async def _reconcile_orphans(self) -> None:
        """重启对账：单进程纪律下，上个进程遗留的 running run 必已死，标记 failed。

        paused_awaiting_confirm 保留——检查点在 PG，confirm 后可恢复（2c DoD 之三）。
        """
        async with session_factory() as db:
            await db.execute(
                text(
                    "UPDATE runs SET status = 'failed', "
                    "error = CAST(:e AS jsonb), finished_at = now() "
                    "WHERE status = 'running'"
                ),
                {"e": json.dumps({"code": "process_restart", "detail": "引擎进程重启，执行中断"})},
            )
            await db.commit()

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

    async def persist_assistant_message(self, conversation_id: str, run_id: str, text: str) -> None:
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
                self._spawn_run_task(str(run_id), self._process_run(str(run_id)))
        elif et == "confirmation":
            run_id = event.get("target_run_id")
            answer = str(event["payload"].get("answer", ""))
            if run_id and answer in ("approved", "rejected"):
                self._spawn_run_task(str(run_id), self._resume_run(str(run_id), answer))
        elif et == "abort":
            run_id = event.get("target_run_id")
            if run_id:
                running = self._run_tasks.get(run_id)
                if running:
                    running.cancel()

    def _spawn_run_task(self, run_id: str, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._run_tasks[run_id] = task
        task.add_done_callback(lambda t: self._run_tasks.pop(run_id, None))

    # ---------- run 执行 ----------

    async def _load_run(self, run_id: str) -> Run | None:
        async with session_factory() as db:
            return await db.get(Run, UUID(run_id))

    async def _process_run(self, run_id: str) -> None:
        """user_input 入口：pending → running → 图执行 → 终态。"""
        try:
            run = await self._load_run(run_id)
            if run is None or run.status != "pending":
                return
            ctx = self._build_ctx(run)
            await self.hooks.on_run_start(ctx)
            await self._set_run_status(run_id, "running")
            await self.emit_event(run_id, "run_status", {"status": "running"})

            await self._invoke_and_finalize(
                run_id,
                str(run.conversation_id) if run.conversation_id else run_id,
                str(run.agent_id),
                input_payload={
                    "messages": [HumanMessage(content=(run.input or {}).get("text", ""))]
                },
            )
        except asyncio.CancelledError:
            await self._finalize_cancelled(run_id)
        except BudgetExceededError as e:
            await self._finalize_budget(run_id, e)
        except TimeoutError:
            await self._set_run_status(
                run_id, "failed", error={"code": "timeout", "detail": "全局超时"}
            )
            await self.emit_event(run_id, "error", {"code": "timeout", "detail": "全局超时"})
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

    async def _resume_run(self, run_id: str, answer: str) -> None:
        """confirmation 入口：从 interrupt 检查点恢复（Command(resume=answer)）。"""
        try:
            run = await self._load_run(run_id)
            if run is None or run.status != "paused_awaiting_confirm":
                return
            # 重启后内存 ctx 丢失：重建（含预算续跑账本）；
            # 同进程恢复时沿用现有 ctx，避免覆盖掉内存中已累计的账本
            if run_id not in self._run_ctx:
                self._build_ctx(run)
            await self._set_run_status(run_id, "running")
            await self.emit_event(
                run_id, "run_status", {"status": "running", "resumed_with": answer}
            )
            await self._invoke_and_finalize(
                run_id,
                str(run.conversation_id) if run.conversation_id else run_id,
                str(run.agent_id),
                input_payload=Command(resume=answer),
            )
        except asyncio.CancelledError:
            await self._finalize_cancelled(run_id)
        except BudgetExceededError as e:
            await self._finalize_budget(run_id, e)
        except TimeoutError:
            await self._set_run_status(
                run_id, "failed", error={"code": "timeout", "detail": "全局超时"}
            )
            await self.emit_event(run_id, "error", {"code": "timeout", "detail": "全局超时"})
            await self.emit_event(run_id, "run_status", {"status": "failed"})
        except Exception as e:  # noqa: BLE001
            logger.exception("run %s resume failed", run_id)
            await self._set_run_status(
                run_id, "failed", error={"code": type(e).__name__, "detail": str(e)[:500]}
            )
            await self.emit_event(
                run_id, "error", {"code": type(e).__name__, "detail": str(e)[:500]}
            )
            await self.emit_event(run_id, "run_status", {"status": "failed"})

    def _build_ctx(self, run: Run) -> RunContext:
        ctx = RunContext(
            str(run.id),
            str(run.conversation_id) if run.conversation_id else None,
            str(run.agent_id),
        )
        budget = run.budget or {}
        ctx.limits = {
            "max_iterations": int(budget.get("max_iterations") or 25),
            "max_tokens_per_run": int(budget.get("max_tokens_per_run") or 0),
            "timeout_seconds": int(budget.get("timeout_seconds") or 600),
        }
        # 重启续跑：从上次实耗恢复账本（DoD：kill 进程后 run 可恢复）
        used = run.budget_used or {}
        restored = {
            k: used.get(k, ctx.budget.get(k, 0))
            for k in ("iterations", "input_tokens", "output_tokens", "tool_calls")
        }
        ctx.budget.update(cast(BudgetState, restored))
        self._run_ctx[str(run.id)] = ctx
        return ctx

    async def _invoke_and_finalize(
        self, run_id: str, thread_id: str, agent_id: str, input_payload: Any
    ) -> None:
        """图执行 + 收尾共用：检测再次 interrupt（暂停）/ 计划拒绝 / 终态落库。"""
        assert self.graph is not None
        run = await self._load_run(run_id)
        timeout = (run.budget or {}).get("timeout_seconds") or 600 if run else 600
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id, "run_id": run_id, "agent_id": agent_id}
        }
        final_state = await asyncio.wait_for(
            self.graph.ainvoke(input_payload, config), timeout=timeout
        )

        # 确认点：图停在 interrupt → run 暂停，等 confirmation 事件恢复
        snapshot = await self.graph.aget_state(config)
        if snapshot.next:
            await self._pause_for_confirmation(run_id, snapshot)
            return

        confirmation = final_state.get("confirmation")
        if confirmation and confirmation.get("answer") == "rejected":
            result = {"text": "用户拒绝了执行计划，任务未执行。"}
            await self._finalize(run_id, "cancelled", result)
            return

        msgs = final_state.get("messages") or []
        final_text = ""
        for m in reversed(msgs):
            if getattr(m, "type", "") == "ai" and m.content:
                final_text = str(m.content)
                break
        await self._finalize(run_id, "done", {"text": final_text})

    async def _pause_for_confirmation(self, run_id: str, snapshot: Any) -> None:
        payload: dict[str, Any] = {"reason": "unknown", "payload": {}}
        for task in snapshot.tasks:
            if task.interrupts:
                value = task.interrupts[0].value
                if isinstance(value, dict):
                    payload = value
                break
        # 暂停即落账本：重启恢复后预算续跑不重置（DoD：kill 后可恢复）
        ctx = self.get_run_ctx(run_id)
        budget_used = {k: v for k, v in ctx.budget.items() if k != "loop_strikes"}
        async with session_factory() as db:
            run = await db.get(Run, UUID(run_id))
            if run:
                run.status = "paused_awaiting_confirm"
                run.budget_used = budget_used
                await db.commit()
        await self.emit_event(run_id, "confirmation_request", payload)
        await self.emit_event(
            run_id,
            "run_status",
            {"status": "paused_awaiting_confirm", "reason": payload.get("reason")},
        )

    async def _finalize(self, run_id: str, status: str, result: dict[str, Any]) -> None:
        ctx = self.get_run_ctx(run_id)
        budget_used = {k: v for k, v in ctx.budget.items() if k != "loop_strikes"}
        async with session_factory() as db:
            run = await db.get(Run, UUID(run_id))
            if run:
                run.status = status
                run.result = result
                run.budget_used = budget_used
                run.finished_at = datetime.now(UTC)
                await db.commit()
        await self.hooks.on_run_end(ctx, status, result)
        await self.emit_event(run_id, "run_status", {"status": status})
        self._run_ctx.pop(run_id, None)

    async def _finalize_cancelled(self, run_id: str) -> None:
        await self._set_run_status(run_id, "cancelled", error={"code": "aborted"})
        await self.emit_event(run_id, "run_status", {"status": "cancelled"})
        self._run_ctx.pop(run_id, None)

    async def _finalize_budget(self, run_id: str, e: BudgetExceededError) -> None:
        await self._set_run_status(
            run_id,
            "failed",
            error={"code": "budget_exceeded", "gate": e.gate, "detail": e.detail},
        )
        await self.emit_event(
            run_id, "error", {"code": "budget_exceeded", "gate": e.gate, "detail": e.detail}
        )
        await self.emit_event(run_id, "run_status", {"status": "failed"})
        self._run_ctx.pop(run_id, None)

    async def _set_run_status(self, run_id: str, status: str, error: dict | None = None) -> None:
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
