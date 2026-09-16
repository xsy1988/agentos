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
import base64
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
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

# 附件注入限制（方案拍板）：小文本直读阈值、单附件提取截断、docling 超时
ATTACHMENT_INLINE_LIMIT = 100 * 1024
ATTACHMENT_TEXT_LIMIT = 30_000
ATTACHMENT_PARSE_TIMEOUT = 60.0


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
            conv = await db.get(Conversation, UUID(conversation_id))
            # 无会话 run（timer 触发）thread_id 是 run_id 兜底，无 Conversation 行：
            # 消息不入 messages 表（run_events + runs.result 已留痕）
            if conv is None:
                return
            db.add(
                Message(
                    conversation_id=conv.id,
                    run_id=UUID(run_id),
                    role="assistant",
                    content={"text": text},
                )
            )
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
            payload = event.get("payload") or {}
            answer = str(payload.get("answer", ""))
            # 侧边栏 plugin 前端的统一结构化回传（§3.5）：data/applied 存在时以 dict 恢复，
            # 供 request_decision 消费；否则沿用字符串答复（approved/rejected/支线文本，ADR-24）
            data = payload.get("data")
            applied = payload.get("applied")
            if run_id and (answer or data is not None):
                resume_value: Any = answer
                if data is not None or applied is not None:
                    resume_value = {"answer": answer, "data": data, "applied": applied}
                self._spawn_run_task(str(run_id), self._resume_run(str(run_id), resume_value))
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

    # ---------- 附件消费（对话附件方案：文档提文本注入，图片多模态投喂） ----------

    async def _build_user_message(self, run: Run) -> HumanMessage:
        """run.input 附件 → HumanMessage。

        图片：多模态 image_url 块（base64 data URL）；模型是否支持视觉由
        graph 侧发送视图按 provider params.vision 适配（此处无条件携带）。
        文档：小文本直读，其余 docling 提取（sha256 缓存）；任何一步失败
        降级为占位文本，不阻断 run。
        """
        inp = run.input or {}
        text = str(inp.get("text") or "")
        att_ids = inp.get("attachment_ids") or []
        if not att_ids:
            return HumanMessage(content=text)

        from app.modules.files.models import File

        doc_parts: list[str] = [text] if text else []
        image_blocks: list[dict[str, Any]] = []
        async with session_factory() as db:
            for fid in att_ids:
                file = await db.get(File, UUID(str(fid)))
                if file is None:
                    doc_parts.append(f"[附件记录缺失：{fid}]")
                    continue
                try:
                    raw = (settings.data_dir / file.path).read_bytes()
                except OSError:
                    doc_parts.append(f"[附件读取失败：{file.filename}]")
                    continue
                if (file.mime or "").startswith("image/"):
                    b64 = base64.b64encode(raw).decode()
                    image_blocks.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{file.mime};base64,{b64}"},
                        }
                    )
                else:
                    doc_parts.append(
                        f"【附件：{file.filename}】\n{await self._extract_doc_text(file, raw)}"
                    )

        full_text = "\n\n".join(p for p in doc_parts if p)
        if image_blocks:
            blocks: list[Any] = [
                {"type": "text", "text": full_text or "（无文本，仅图片）"}
            ] + image_blocks
            return HumanMessage(content=blocks)
        return HumanMessage(content=full_text)

    async def _extract_doc_text(self, file: Any, raw: bytes) -> str:
        """文档附件 → 注入文本：txt/md 小文件直读，其余 docling 提取（缓存）。"""
        ext = Path(str(file.path)).suffix.lower()
        if ext in (".txt", ".md") and len(raw) <= ATTACHMENT_INLINE_LIMIT:
            return raw.decode("utf-8", errors="replace")[:ATTACHMENT_TEXT_LIMIT]

        # docling 提取，sha256 键缓存（同一文件重复发送不重解析）
        cache = settings.data_dir / "parse" / f"att-{str(file.sha256 or file.id)[:16]}.md"
        if cache.is_file():
            md = cache.read_text(encoding="utf-8")
        else:
            from app.modules.knowledge.parser import parse_document

            try:
                md = await asyncio.wait_for(
                    parse_document(str(file.filename), raw),
                    timeout=ATTACHMENT_PARSE_TIMEOUT,
                )
            except Exception as e:  # noqa: BLE001 —— 解析失败降级占位，不阻断对话
                return f"[附件解析失败：{file.filename}（{type(e).__name__}）]"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(md, encoding="utf-8")
        if len(md) > ATTACHMENT_TEXT_LIMIT:
            md = md[:ATTACHMENT_TEXT_LIMIT] + "\n…（内容过长已截断）"
        return md

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
                input_payload={"messages": [await self._build_user_message(run)]},
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

    async def _resume_run(self, run_id: str, answer: Any) -> None:
        """confirmation 入口：从 interrupt 检查点恢复（Command(resume=answer)）。

        answer 可为字符串（approved/rejected/支线文本答复）或 dict（侧边栏结构化
        回传 {answer, data, applied}，§3.5）——原样注入 Command(resume=...)，由对应 interrupt 消费。
        """
        try:
            run = await self._load_run(run_id)
            if run is None or run.status != "paused_awaiting_confirm":
                return
            # 重启后内存 ctx 丢失：重建（含预算续跑账本）；
            # 同进程恢复时沿用现有 ctx，避免覆盖掉内存中已累计的账本
            if run_id not in self._run_ctx:
                self._build_ctx(run)
            await self._set_run_status(run_id, "running")
            resumed_with = answer.get("answer", "") if isinstance(answer, dict) else answer
            await self.emit_event(
                run_id,
                "run_status",
                {"status": "running", "resumed_with": str(resumed_with)[:200]},
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
        # 任务架构（M7a）：run 从创建时就快照了 task_id（send_message 写入 run.input）；
        # 迁移前的老 run 用会话惰性补建，保证任何 run 都能挂到主任务上。
        task_id = (run.input or {}).get("task_id") if run else None
        if not task_id and run is not None and run.conversation_id:
            task_id = await self.backend.ensure_task_id(str(run.conversation_id))
        config: RunnableConfig = {
            "configurable": {
                "thread_id": thread_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "task_id": str(task_id) if task_id else None,
                # trigger 供确认门区分人审场景（timer 无人值守，计划确认自动通过）
                "trigger": run.trigger if run else None,
                # 对话内临时换模型（run 级覆盖，随 run.input 快照固化）
                "model_provider_id": (run.input or {}).get("model_provider_id") if run else None,
            }
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
        # 任务架构：只有「本 run 确实把任务做完了」才推进主线子任务。
        # 闲聊不推进（用户在任务会话里插一句寒暄不该算完成一个子任务）；
        # 复杂任务以验收结论为准（verify 未达成时预算已耗尽才结束，此时不推进）。
        protected = final_state.get("protected_context") or {}
        verdict = protected.get("verify_verdict") or {}
        achieved = protected.get("intent", "task") != "chitchat" and bool(
            verdict.get("achieved", True)
        )
        await self._finalize(run_id, "done", {"text": final_text}, achieved=achieved)

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

    async def _finalize(
        self, run_id: str, status: str, result: dict[str, Any], *, achieved: bool = True
    ) -> None:
        ctx = self.get_run_ctx(run_id)
        budget_used = {k: v for k, v in ctx.budget.items() if k != "loop_strikes"}
        task_id: str | None = None
        async with session_factory() as db:
            run = await db.get(Run, UUID(run_id))
            if run:
                run.status = status
                run.result = result
                run.budget_used = budget_used
                run.finished_at = datetime.now(UTC)
                await db.commit()
                task_id = (run.input or {}).get("task_id")
                if not task_id and run.conversation_id:
                    task_id = await self.backend.ensure_task_id(str(run.conversation_id))
                # 通知路由（M5，模块详细设计 §3）：无会话归属的 run（定时任务）
                # 没有实时推送面 → 进通知中心；有会话的靠 SSE 实时推送
                if run.conversation_id is None and status in ("done", "failed"):
                    from app.modules.notifications.models import Notification

                    db.add(
                        Notification(
                            run_id=run.id,
                            kind="run_done" if status == "done" else "run_failed",
                            title=f"定时任务{'完成' if status == 'done' else '失败'}",
                            content=(result.get("text") or "")[:2000],
                        )
                    )
                    await db.commit()
        # 任务架构回写（ADR-26）：把本次 run 完成的主线子任务推进到 done，
        # 主线收口后进度重算会自动跳过未触发的支线并把主任务置 done。
        if task_id and status == "done":
            try:
                await self.backend.finalize_task_plan(task_id, run_id, achieved=achieved)
                from app.modules.engine.graph import emit_task_steps

                await emit_task_steps(self, task_id, run_id)
            except Exception:  # noqa: BLE001 —— 任务回写失败不能影响 run 落库
                logger.exception("run %s 任务回写失败", run_id)
        elif task_id:
            # 非正常终态（如用户拒绝计划 → cancelled）：收敛挂起的待确认支线
            await self._reconcile_awaiting_steps(run_id)
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

    async def _reconcile_awaiting_steps(self, run_id: str) -> None:
        """非正常终态收敛：挂在本 run 上仍 awaiting_user 的支线置 blocked。

        run 已结束没人会再答复，不收敛则看板永久误报「待确认」。
        正常 done 的 run 不走此处（由答复回填/进度重算自然处理）。
        """
        try:
            from app.modules.tasks import service as tasks_service
            from app.modules.tasks.models import Task

            async with session_factory() as db:
                run = await db.get(Run, UUID(run_id))
                if run is None:
                    return
                task_id = (run.input or {}).get("task_id")
                if not task_id and run.conversation_id:
                    task_id = await self.backend.ensure_task_id(str(run.conversation_id))
                if not task_id:
                    return
                task = await db.get(Task, UUID(str(task_id)))
                if task is None:
                    return
                closed = await tasks_service.reconcile_orphaned_awaits(
                    db, task, run_id=UUID(run_id)
                )
                await db.commit()
                if closed:
                    logger.info("run %s 终态收敛 %d 个待确认支线 → blocked", run_id, closed)
        except Exception:  # noqa: BLE001 —— 收敛失败不影响终态落库
            logger.exception("run %s 待确认支线收敛失败", run_id)

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
        # 非正常终态（failed/cancelled/aborted/timeout）：收敛挂起的待确认支线
        if status in ("failed", "cancelled", "aborted", "timeout"):
            await self._reconcile_awaiting_steps(run_id)


# 单例（main.py lifespan 中 start/stop）
engine_runtime = EngineRuntime()
