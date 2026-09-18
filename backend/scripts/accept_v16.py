"""v1.6 真实验收：方案 §9.1（V1–V14）里能用代码判定的那部分。

与 `tests/` 的分工写清楚，避免重复劳动也避免自欺：

- `tests/` 在**假 DB + 假图**上钉不变量（快、边界可枚举）；
- 本脚本在**真库（pgvector/pg17）+ 真 HTTP 栈（ASGITransport，不起 uvicorn）+
  受控假图**上跑端到端，只信库里的行与 HTTP 响应体。凡单测用 stub 顶掉的那层
  （真实列与约束、真实唯一索引、真实 JSONB、真实回调鉴权与 CAS），这里都真跑一遍。

判据：**实测不通过就改代码，不改断言**。任何一条 FAIL 让进程以退出码 1 结束；
WARN 是"今日无害、上量前要处理"的观察项，不影响退出码。

用法::

    uv run python -m scripts.accept_v16                    # 全部
    uv run python -m scripts.accept_v16 --list             # 看清单
    uv run python -m scripts.accept_v16 -g ledger await    # 选分组
    uv run python -m scripts.accept_v16 --keep             # 保留探针数据（排障用）
    DATABASE_URL=postgresql+asyncpg://... uv run python -m scripts.accept_v16

脚本只增不改：探针数据都带 `accept-v16-` 前缀，结束时按依赖序清理干净。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------- 结果记录 ----------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


@dataclass
class Case:
    no: str
    title: str
    expected: str
    actual: str
    status: str = PASS
    evidence: list[str] = field(default_factory=list)


class Report:
    def __init__(self) -> None:
        self.cases: list[Case] = []

    def add(
        self,
        no: str,
        title: str,
        *,
        expected: str,
        actual: str,
        passed: bool,
        evidence: list[str] | None = None,
        warn_only: bool = False,
    ) -> Case:
        case = Case(
            no=no,
            title=title,
            expected=expected,
            actual=actual,
            status=(PASS if passed else (WARN if warn_only else FAIL)),
            evidence=evidence or [],
        )
        self.cases.append(case)
        mark = {PASS: "✅", FAIL: "❌", WARN: "⚠️ "}[case.status]
        print(f"{mark} [{case.no}] {case.title}")
        print(f"     期望：{case.expected}")
        print(f"     实测：{case.actual}")
        for line in case.evidence:
            print(f"     证据：{line}")
        return case

    @property
    def failed(self) -> list[Case]:
        return [c for c in self.cases if c.status == FAIL]

    def render(self) -> str:
        rows = [f"| {self._cell(c)} |" for c in self.cases]
        head = "\n| 编号 | 结果 | 条目 | 期望 | 实测 |\n|---|---|---|---|---|"
        counts = {s: len([c for c in self.cases if c.status == s]) for s in (PASS, FAIL, WARN)}
        tail = (
            f"\n合计 {len(self.cases)} 条："
            f"PASS {counts[PASS]} / FAIL {counts[FAIL]} / WARN {counts[WARN]}"
        )
        return head + "\n" + "\n".join(rows) + tail

    @staticmethod
    def _cell(c: Case) -> str:
        return f"{c.no} | {c.status} | {c.title} | {c.expected} | {c.actual}"


# ---------- 受控假图 ----------


@dataclass
class _Intr:
    value: dict[str, Any]


@dataclass
class _Task:
    interrupts: list[_Intr]


@dataclass
class _Msg:
    """假消息：只需满足 runtime/hooks 读 `type` 与 `content`。"""

    type: str
    content: str


class _Snapshot:
    def __init__(self, *, pending: bool = False, interrupt: dict[str, Any] | None = None) -> None:
        self.next: tuple[str, ...] = ("__pending__",) if pending else ()
        self.values: dict[str, Any] = {"messages": []}
        self.tasks = [_Task([_Intr(interrupt)])] if interrupt is not None else []

    def with_text(self, text: str) -> _Snapshot:
        self.values = {"messages": [_Msg("ai", text)]}
        return self

    def carrying(self, values: dict[str, Any]) -> _Snapshot:
        """清空挂起态（`next`/`tasks`）但保留已持久化的 values —— 检查点是持久的。"""
        self.values = values
        return self

    @staticmethod
    def awaiting(payload: dict[str, Any]) -> _Snapshot:
        """图停在 `external_await` interrupt 上（真实工具落定后就是这么停的）。"""
        return _Snapshot(pending=True, interrupt={"reason": "external_await", "payload": payload})


class _FakeGraph:
    """按调用序执行脚本步骤的假图；步骤可睡眠、可切快照、可返回任意终态。"""

    def __init__(self, steps: list[Any]) -> None:
        self.steps = steps
        self.calls = 0
        self.snapshot = _Snapshot()
        self.last_config: Any = None

    async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
        # 一次正常返回 = 图没停在 interrupt 上：清掉挂起标记，但**保留已落进
        # values 的消息**（真实图检查点是持久的，`_partial_text` 正靠它捞部分成果）。
        self.snapshot = _Snapshot().carrying(self.snapshot.values)
        self.last_config = config
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        result = step(self)
        return await result if asyncio.iscoroutine(result) else result

    async def aget_state(self, config: Any) -> _Snapshot:
        return self.snapshot


def _done(text: str) -> dict[str, Any]:
    """终态：无 interrupt、带 AI 终答 → runtime 走 `_finalize(done)`。"""
    return {"messages": [_Msg("ai", text)]}


# ---------- 夹具 ----------

_OPEN_TOKEN = "accept-v16-open-token"
_TAG = "accept-v16"
_CLEANUP_TABLES: tuple[tuple[str, str], ...] = (
    ("run_artifacts", "run_id"),
    ("run_events", "run_id"),
    ("inbox_events", "target_run_id"),
    ("await_broker", "run_id"),
)


def _prepare_env() -> None:
    """必须在 import `app.*` 之前：开放接口鉴权取自 settings（进程启动时读一次）。"""
    os.environ.setdefault("OPEN_API_TOKEN", _OPEN_TOKEN)


class Harness:
    def __init__(self, *, keep: bool) -> None:
        self.keep = keep
        self.report = Report()
        self.user_id = ""
        self.agent_id = ""
        self.conversation_id = ""
        self.extra_conversations: list[str] = []
        self.run_ids: list[str] = []
        # 真实 MCP 对账（-g mcp）留下的探针：能力行、文件行、磁盘文件、池内会话
        self.capability_ids: list[str] = []
        # 容量预警（-g budget）自建的目标 Agent 行，跑完删除
        self.extra_agent_ids: list[str] = []
        self.file_ids: list[str] = []
        self.probe_paths: list[Any] = []
        self.mcp_conns: list[Any] = []
        self.client: Any = None
        self.rt: Any = None
        self.sf: Any = None
        self._text: Any = None

    # -- 生命周期 --

    async def setup(self) -> None:
        import httpx
        from sqlalchemy import text

        from app.core.db import session_factory
        from app.core.security import create_access_token, hash_password
        from app.main import app
        from app.modules.agents.models import Agent
        from app.modules.auth.models import User
        from app.modules.conversations.models import Conversation
        from app.modules.engine.hooks_impl import build_default_chain
        from app.modules.engine.runtime import EngineRuntime

        self.sf = session_factory
        self._text = text
        async with session_factory() as db:
            user = User(
                username=f"{_TAG}-{uuid.uuid4().hex[:8]}",
                password_hash=hash_password("accept-v16"),
            )
            db.add(user)
            await db.flush()
            self.user_id = str(user.id)
            # 模型供应商：借用库里现有的（Agent 未绑模型时上下文装配会缺 provider）
            provider_id = await db.scalar(
                text("SELECT id FROM model_providers WHERE status = 'enabled' LIMIT 1")
            )
            agent = Agent(name=_TAG, model_provider_id=provider_id)
            db.add(agent)
            await db.flush()
            self.agent_id = str(agent.id)
            conv = Conversation(agent_id=agent.id, title=_TAG, status="active")
            db.add(conv)
            await db.flush()
            self.conversation_id = str(conv.id)
            await db.commit()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://accept.v16",
            headers={"Authorization": f"Bearer {create_access_token(self.user_id)}"},
        )
        self.rt = EngineRuntime()  # 不起后台循环：只手动驱动执行路径
        self.rt.hooks = build_default_chain(self.rt.emit_event)

    async def teardown(self) -> None:
        await self._close_mcp_conns()
        if self.client is not None:
            await self.client.aclose()
        if self.keep or self.sf is None:
            return
        convs = [c for c in [self.conversation_id, *self.extra_conversations] if c]
        if not convs:
            return
        async with self.sf() as db:
            if self.run_ids:
                for table, column in _CLEANUP_TABLES:
                    await db.execute(
                        self._text(
                            f"DELETE FROM {table} WHERE {column} = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": self.run_ids},
                    )
            for table, column in (
                ("messages", "conversation_id"),
                ("runs", "conversation_id"),
                ("tasks", "conversation_id"),
                ("conversations", "id"),
            ):
                await db.execute(
                    self._text(f"DELETE FROM {table} WHERE {column} = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": convs},
                )
            for table in ("agents", "users"):
                await db.execute(
                    self._text(f"DELETE FROM {table} WHERE id = :id"),
                    {"id": self.agent_id if table == "agents" else self.user_id},
                )
            if self.extra_agent_ids:
                await db.execute(
                    self._text("DELETE FROM agents WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": self.extra_agent_ids},
                )
            if self.capability_ids:
                # capability_tools / capability_bindings 都是 ondelete=CASCADE
                await db.execute(
                    self._text("DELETE FROM capabilities WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": self.capability_ids},
                )
            if self.file_ids:
                await db.execute(
                    self._text("DELETE FROM files WHERE id = ANY(CAST(:ids AS uuid[]))"),
                    {"ids": self.file_ids},
                )
            await db.commit()
        for path in self.probe_paths:
            with contextlib.suppress(OSError):
                path.unlink()
        self.probe_paths.clear()

    async def _close_mcp_conns(self) -> None:
        """断开探针 MCP 会话。

        `_conns` 是池的私有映射：探针**故意**插进去，为的是让 `list_enabled_tools` /
        `call_tool` 走与生产完全相同的代码路径（而不是自己另开一条捷径）。
        """
        if not self.mcp_conns:
            return
        from app.modules.capabilities.mcp_client import mcp_pool

        for cap_id in self.mcp_conns:
            conn = mcp_pool._conns.pop(cap_id, None)
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
        self.mcp_conns.clear()

    # -- 探针数据 --

    async def seed_run(
        self,
        *,
        status: str = "pending",
        timeout_seconds: int = 60,
        task_id: str | None = None,
        worker_name: str | None = None,
        client_message_id: str | None = None,
        legacy_run: bool = False,
        inputs: dict[str, Any] | None = None,
        attachment_ids: list[str] | None = None,
        trigger: str = "manual",
        conversation_id: str | None = None,
        text: str | None = None,
    ) -> str:
        from app.modules.runs.models import Run
        from app.modules.tasks import service as tasks_service
        from app.modules.workers.registry import COMMON_WORKER

        run_id = uuid.uuid4()
        conv_id = conversation_id or self.conversation_id
        payload: dict[str, Any] = {
            # 走真图的用例必须给"有实质诉求"的文本：`intent_router` 的轻量分类对
            # 无意义短语会判 chitchat，而 chitchat 直连 agent、绕过 context_assembly
            # 与 input_gate（门是"任务"路径上的节点）——那不是门的问题。
            "text": text or f"{_TAG} 探针",
            "attachment_ids": list(attachment_ids or []),
            "model_provider_id": None,
        }
        if client_message_id:
            payload["client_message_id"] = client_message_id
        if inputs:
            payload["inputs"] = inputs
        async with self.sf() as db:
            if legacy_run:
                # 迁移前的老 run：input 里没有 task_id，执行期只能走会话惰性补建
                pass
            else:
                task = task_id or await self._ensure_task(db, tasks_service, COMMON_WORKER, conv_id)
                payload["task_id"] = task
                payload["worker_name"] = worker_name or COMMON_WORKER
            db.add(
                Run(
                    id=run_id,
                    conversation_id=uuid.UUID(conv_id),
                    agent_id=uuid.UUID(self.agent_id),
                    trigger=trigger,
                    status=status,
                    input=payload,
                    budget={
                        "tool_budget": 8,
                        "max_iterations": 5,
                        "max_tokens_per_run": None,
                        "timeout_seconds": timeout_seconds,
                    },
                    budget_used={},
                    active_ms=0,
                )
            )
            await db.commit()
        self.run_ids.append(str(run_id))
        return str(run_id)

    async def _ensure_task(
        self,
        db: Any,
        tasks_service: Any,
        worker_name: str,
        conversation_id: str | None = None,
    ) -> str:
        """会话↔主任务 1:1（生产由 send_message 保证），这里复刻同一不变量。"""
        from app.modules.tasks.service import get_task_by_conversation

        conv_id = uuid.UUID(conversation_id or self.conversation_id)
        task = await get_task_by_conversation(db, conv_id)
        if task is None:
            task = await tasks_service.create_task(
                db,
                worker_name=worker_name,
                agent_id=uuid.UUID(self.agent_id),
                title=_TAG,
                conversation_id=conv_id,
            )
        return str(task.id)

    def track_run(self, run_id: Any) -> str:
        """登记 API 侧新建的 run（teardown 按 run 清事件/产物）。"""
        text = str(run_id)
        if text not in self.run_ids:
            self.run_ids.append(text)
        return text

    async def seed_file(self, filename: str) -> str:
        """只落一行文件登记：`file` 输入的附件顶替只看文件名，不读磁盘。"""
        from app.modules.files.models import File

        async with self.sf() as db:
            row = File(
                path=f"data/files/{_TAG}/{uuid.uuid4().hex}.xlsx",
                filename=filename,
                mime="application/vnd.ms-excel",
                size=2048,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            )
            db.add(row)
            await db.commit()
            file_id = str(row.id)
        self.file_ids.append(file_id)
        return file_id

    async def new_conversation(self) -> str:
        """另开一个会话并登记清理：让需要「干净主任务」的分组互不干扰。"""
        from app.modules.conversations.models import Conversation

        async with self.sf() as db:
            conv = Conversation(
                agent_id=uuid.UUID(self.agent_id), title=f"{_TAG} 分组探针", status="active"
            )
            db.add(conv)
            await db.commit()
            conv_id = str(conv.id)
        self.extra_conversations.append(conv_id)
        return conv_id

    async def task_id(self, conversation_id: str | None = None) -> str:
        """取（必要时按生产不变量补建）会话的主任务 id。"""
        from app.modules.tasks import service as tasks_service
        from app.modules.workers.registry import COMMON_WORKER

        async with self.sf() as db:
            task_id = await self._ensure_task(db, tasks_service, COMMON_WORKER, conversation_id)
            await db.commit()
        return task_id

    async def seed_step(
        self,
        task_id: str,
        *,
        seq: int,
        name: str,
        status: str = "pending",
        kind: str = "branch",
        run_id: str | None = None,
        worker_step_ref: str | None = None,
    ) -> str:
        """直落一条子任务实例。状态迁移只走 `update_step_status`，这里仅造初始数据。"""
        from app.modules.tasks.models import TaskStep

        async with self.sf() as db:
            step = TaskStep(
                task_id=uuid.UUID(task_id),
                seq=seq,
                name=name,
                kind=kind,
                status=status,
                source="agent_raised",
                worker_step_ref=worker_step_ref,
                run_id=uuid.UUID(run_id) if run_id else None,
            )
            db.add(step)
            await db.commit()
            return str(step.id)

    async def refresh_progress(self, task_id: str) -> dict[str, Any]:
        """按生产入口重算反范式进度列。

        `autoclose=False`：探针要观察的是"某次动作之后进度如何"，不该被顺手的自动收口
        掩盖——收口语义由专门的用例单独判。
        """
        from app.modules.tasks import service as tasks_service
        from app.modules.tasks.models import Task

        async with self.sf() as db:
            task = await db.get(Task, uuid.UUID(task_id))
            assert task is not None
            await tasks_service.recompute_progress(db, task, autoclose=False)
            await db.commit()
            return {
                "done": task.progress_done,
                "total": task.progress_total,
                "status": task.status,
            }

    async def step_status(self, step_id: str, status: str) -> None:
        """走生产状态迁移入口改子任务状态（进度随之重算）。"""
        from app.modules.tasks import service as tasks_service
        from app.modules.tasks.models import TaskStep

        async with self.sf() as db:
            step = await db.get(TaskStep, uuid.UUID(step_id))
            assert step is not None
            await tasks_service.update_step_status(db, step, status)
            await db.commit()

    async def step_row(self, step_id: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT id, seq, name, kind, status, run_id, resolution, raised_at, resolved_at "
            "FROM task_steps WHERE id = :id",
            {"id": step_id},
        )
        assert row is not None
        return row

    async def task_row(self, task_id: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT id, status, progress_done, progress_total, finished_at FROM tasks "
            "WHERE id = :id",
            {"id": task_id},
        )
        assert row is not None
        return row

    async def events_of(self, run_id: str, event_type: str) -> list[dict[str, Any]]:
        return [e for e in await self.events(run_id) if e["event_type"] == event_type]

    async def register_await(
        self,
        run_id: str,
        *,
        tool_name: str = "accept.echo",
        args: dict[str, Any] | None = None,
        timeout_seconds: int | None = 30,
    ) -> tuple[dict[str, Any], bool]:
        """复刻 `graph.await_gate` 的第一半：幂等登记等待行，返回 (interrupt 值, created)。

        真实路径里这一段发生在图内；假图把它显式调出来，才能同时验证幂等与落库。
        """
        from app.modules.awaits import service as awaits_service

        args = args or {"q": "accept"}
        key = awaits_service.make_idempotency_key(args)
        async with self.sf() as db:
            row, created = await awaits_service.ensure_await(
                db,
                run_id=run_id,
                tool_name=tool_name,
                idempotency_key=key,
                timeout_seconds=timeout_seconds,
                conversation_id=self.conversation_id,
                payload_in={"args": args},
            )
            return (
                {
                    "await_id": str(row.id),
                    "tool": tool_name,
                    "idempotency_key": key,
                    "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
                    "args": args,
                    "kind": "await",
                },
                created,
            )

    async def callback(
        self,
        await_id: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        api_key: str | None = _OPEN_TOKEN,
    ) -> Any:
        """打真实回调入口（`POST /api/v1/open/awaits/{id}/resolve`）。"""
        from app.modules.awaits import service as awaits_service

        body: dict[str, Any] = {
            "callback_token": token
            if token is not None
            else awaits_service.make_callback_token(await_id),
            "payload": payload if payload is not None else {"ok": True, "rows": 3},
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        headers = {"X-API-Key": api_key} if api_key is not None else {}
        return await self.client.post(
            f"/api/v1/open/awaits/{await_id}/resolve", json=body, headers=headers
        )

    async def resume_payload(self, await_id: str) -> dict[str, Any]:
        """唤醒载荷 = 引擎 worker 从 inbox 捞到的那一份（平台自身函数现算）。"""
        from app.modules.awaits import service as awaits_service

        async with self.sf() as db:
            row = await db.get(  # type: ignore[arg-type]
                awaits_service.AwaitBroker, uuid.UUID(await_id)
            )
            assert row is not None
            return awaits_service.resume_payload(
                row, status=row.status, payload=row.payload_out, error=row.error
            )

    async def count(self, sql: str, params: dict[str, Any]) -> int:
        return int(await self._scalar(sql, params))

    async def _scalar(self, sql: str, params: dict[str, Any]) -> Any:
        async with self.sf() as db:
            return await db.scalar(self._text(sql), params)

    async def _one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        async with self.sf() as db:
            row = (await db.execute(self._text(sql), params)).mappings().first()
        return dict(row) if row is not None else None

    async def fetch_run(self, run_id: str) -> dict[str, Any]:
        row = await self._one(
            "SELECT id, status, started_at, finished_at, deadline_at, active_ms, result, error, "
            "budget_used FROM runs WHERE id = :id",
            {"id": run_id},
        )
        assert row is not None
        return row

    async def run_input(self, run_id: str) -> dict[str, Any]:
        row = await self._one("SELECT input FROM runs WHERE id = :id", {"id": run_id})
        assert row is not None
        return row["input"] or {}

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        async with self.sf() as db:
            rows = (
                await db.execute(
                    self._text(
                        "SELECT seq, event_type, payload FROM run_events "
                        "WHERE run_id = :id ORDER BY seq"
                    ),
                    {"id": run_id},
                )
            ).mappings()
        return [dict(r) for r in rows.all()]

    async def await_row(self, await_id: str) -> dict[str, Any] | None:
        return await self._one(
            "SELECT id, run_id, tool_name, idempotency_key, status, deadline_at, resolved_at, "
            "waited_ms, attempts, error, payload_out FROM await_broker WHERE id = :id",
            {"id": await_id},
        )

    async def inbox(self, run_id: str) -> list[dict[str, Any]]:
        """本 run 的收件箱事件，按 `id` 升序——这也是 `_claim_next` 的认领顺序。"""
        async with self.sf() as db:
            rows = (
                await db.execute(
                    self._text(
                        "SELECT id, event_type, payload, status FROM inbox_events "
                        "WHERE target_run_id = :id ORDER BY id"
                    ),
                    {"id": run_id},
                )
            ).mappings()
        return [dict(r) for r in rows.all()]

    async def claim_next_for(self, run_id: str) -> dict[str, Any] | None:
        """认领**本 run** 的下一条 `new` 事件（认领协议与 `EngineRuntime._claim_next` 一致）。

        必须限定 run：验收脚本是手动驱动（`_process_run` / `_handle`），运行期的 inbox
        worker 没在跑，别的分组留下的 `new` 事件（如等待到期投的 `resume`）不会被消费，
        全局认领会先把它们抓走，于是本 run 的 confirmation 被跳过、恢复根本没发生。
        """
        async with self.sf() as db:
            row = (
                (
                    await db.execute(
                        self._text(
                            "UPDATE inbox_events SET status = 'consumed', consumed_at = now() "
                            "WHERE id = (SELECT id FROM inbox_events WHERE status = 'new' "
                            "AND target_run_id = :rid ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                            "RETURNING id, event_type, target_run_id, payload"
                        ),
                        {"rid": run_id},
                    )
                )
                .mappings()
                .first()
            )
            await db.commit()
        if row is None:
            return None
        payload = row["payload"]
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "target_run_id": str(row["target_run_id"]) if row["target_run_id"] else None,
            "payload": json.loads(payload) if isinstance(payload, str) else (payload or {}),
        }


# ---------- V3：时长账本（P0-1）----------


async def v3_ledger(h: Harness) -> None:
    """账本三件事：started_at/deadline_at 一次写入、active_ms 只累计执行段、API 可读。"""
    run_id = await h.seed_run(timeout_seconds=60)
    h.rt.graph = _FakeGraph([lambda _g: _done("验收：正常终态")])
    await h.rt._process_run(run_id)
    row = await h.fetch_run(run_id)

    started, deadline, active = row["started_at"], row["deadline_at"], row["active_ms"]
    span = (deadline - started).total_seconds() if started and deadline else -1.0
    h.report.add(
        "V3-ledger",
        "跑完一个 run 后账本落库，且 deadline = started_at + 预算",
        expected="status=done、started_at/deadline_at 非空、deadline-started=60s、active_ms>0",
        actual=f"status={row['status']}、deadline-started={span:.2f}s、active_ms={active}",
        passed=(
            row["status"] == "done"
            and started is not None
            and 59.0 <= span <= 61.0
            and (active or 0) > 0
        ),
    )
    metrics = (row["result"] or {}).get("metrics") or {}
    h.report.add(
        "V3-metrics",
        "结果信封带 metrics，且 elapsed_ms ≥ active_ms > 0",
        expected="schema=run_result/v1、metrics.elapsed_ms ≥ metrics.active_ms > 0",
        actual=f"schema={(row['result'] or {}).get('schema')}、"
        f"elapsed={metrics.get('elapsed_ms')}、active={metrics.get('active_ms')}",
        passed=(
            (row["result"] or {}).get("schema") == "run_result/v1"
            and (metrics.get("elapsed_ms") or 0) >= (metrics.get("active_ms") or 0) > 0
        ),
    )

    resp = await h.client.get(f"/api/v1/runs/{run_id}")
    body = resp.json() if resp.status_code == 200 else {}
    h.report.add(
        "V3-api",
        "前端能直接读到时长字段（GET /runs/{id}）",
        expected="200 且 deadline_at 非空、active_ms 与库一致、elapsed_ms ≥ active_ms",
        actual=(
            f"{resp.status_code}、deadline_at={body.get('deadline_at')}、"
            f"active_ms={body.get('active_ms')}、elapsed_ms={body.get('elapsed_ms')}"
        ),
        passed=(
            resp.status_code == 200
            and bool(body.get("deadline_at"))
            and body.get("active_ms") == active
            and (body.get("elapsed_ms") or 0) >= (body.get("active_ms") or 0)
        ),
    )


# ---------- V1 / V2：分段超时与部分成果 ----------


async def v1_segmented_timeout(h: Harness) -> None:
    """预算 3s：段 1 真跑 1.2s 后暂停，隔 0.6s 恢复，段 2 要到不存在的时间。

    判据是**墙钟**：若 deadline 被重置成满额，段 2 会活到 1.2+0.6+3=4.8s；
    正确语义（暂停顺延、执行照扣）下整轮 ≈ budget + gap = 3.6s 收尾。
    """
    budget, seg1_work, gap = 3, 1.2, 0.6
    run_id = await h.seed_run(timeout_seconds=budget)

    async def step1(g: _FakeGraph) -> dict[str, Any]:
        await asyncio.sleep(seg1_work)
        g.snapshot = _Snapshot(
            pending=True, interrupt={"reason": "plan_confirm", "payload": {}}
        ).with_text("段 1 已完成的部分结论")
        return {"messages": [_Msg("ai", "段 1 已完成的部分结论")]}

    async def step2(_g: _FakeGraph) -> dict[str, Any]:
        await asyncio.sleep(budget + 30)  # 想睡到天荒地老，只能被截止点砍掉
        return _done("不应到达")

    h.rt.graph = _FakeGraph([step1, step2])
    t0 = time.monotonic()
    await h.rt._process_run(run_id)
    paused = await h.fetch_run(run_id)
    d1 = paused["deadline_at"]

    h.report.add(
        "V1-seg1",
        "段 1 停在 interrupt → 暂停态与账本同一次提交落库",
        expected="status=paused_awaiting_confirm、deadline-started=3s、active_ms≈1200",
        actual=f"status={paused['status']}、"
        f"deadline-started={(d1 - paused['started_at']).total_seconds():.2f}s、"
        f"active_ms={paused['active_ms']}",
        passed=(
            paused["status"] == "paused_awaiting_confirm"
            and abs((d1 - paused["started_at"]).total_seconds() - budget) < 0.01
            and 1100 <= (paused["active_ms"] or 0) <= 1600
        ),
    )

    await asyncio.sleep(gap)
    await h.rt._resume_run(run_id, "approved")
    wall = time.monotonic() - t0
    row = await h.fetch_run(run_id)
    d2 = row["deadline_at"]
    metrics = (row["result"] or {}).get("metrics") or {}
    extension = (d2 - d1).total_seconds()

    h.report.add(
        "V1-no-reset",
        "恢复后不重取满额预算（墙钟判据）",
        expected=f"整轮墙钟 ≤ {budget + gap + 0.8:.1f}s（重置则是 {seg1_work + gap + budget:.1f}s+）",
        actual=f"整轮墙钟={wall:.2f}s、deadline 顺延={extension:.2f}s",
        passed=wall <= budget + gap + 0.8 and 0.3 <= extension <= gap + 0.3,
        evidence=[f"deadline 总量={(d2 - row['started_at']).total_seconds():.2f}s"],
    )

    h.report.add(
        "V1-timeout",
        "超时收尾：结构化失败 + 部分结果，result 不为 NULL",
        expected="status=failed、error.code=timeout、result.outcome=partial、result.partial=true",
        actual=f"status={row['status']}、error.code={(row['error'] or {}).get('code')}、"
        f"outcome={(row['result'] or {}).get('outcome')}、"
        f"partial={(row['result'] or {}).get('partial')}",
        passed=(
            row["status"] == "failed"
            and (row["error"] or {}).get("code") == "timeout"
            and (row["result"] or {}).get("outcome") == "partial"
            and (row["result"] or {}).get("partial") is True
        ),
    )

    active, elapsed = metrics.get("active_ms"), metrics.get("elapsed_ms")
    h.report.add(
        "V3-pause-not-counted",
        "账本口径：active_ms 只算执行段，暂停时长不计入",
        expected=f"active_ms≈{budget * 1000}（预算跑满）、elapsed-active≈{gap * 1000:.0f}ms",
        actual=f"active_ms={active}、elapsed_ms={elapsed}、差={None if None in (active, elapsed) else elapsed - active}",
        passed=(
            active is not None
            and elapsed is not None
            and budget * 1000 - 400 <= active <= budget * 1000 + 400
            and abs((elapsed - active) - gap * 1000) <= 400
        ),
    )

    h.report.add(
        "V2-partial-kept",
        "超时保留已产出的部分成果（从检查点捞回 AI 文本）",
        expected="result.text 含段 1 的部分结论",
        actual=f"text={(row['result'] or {}).get('text')!r}",
        passed="段 1 已完成的部分结论" in str((row["result"] or {}).get("text") or ""),
    )


# ---------- V6 / V7 / V8 / V9：外部等待与回调 ----------


async def v6_await_roundtrip(h: Harness) -> None:
    """真实等待-回调闭环：INVOKE → interrupt → 回调落定 → 唤醒 → 继续到终态。"""
    run_id = await h.seed_run(timeout_seconds=60)
    holder: dict[str, Any] = {}

    async def step_awaiting(g: _FakeGraph) -> dict[str, Any]:
        payload, created = await h.register_await(
            run_id, args={"q": "accept-v6"}, timeout_seconds=30
        )
        holder["payload"] = payload
        holder["created"] = created
        g.snapshot = _Snapshot.awaiting(payload)
        return {"messages": [_Msg("ai", "已派发外部流程，等待回传")]}

    async def step_final(_g: _FakeGraph) -> dict[str, Any]:
        return _done("外部结果已并入，任务完成")

    h.rt.graph = _FakeGraph([step_awaiting, step_final])
    await h.rt._process_run(run_id)

    paused = await h.fetch_run(run_id)
    events = await h.events(run_id)
    await_id = holder["payload"]["await_id"]
    row = await h.await_row(await_id)
    started = next((e for e in events if e["event_type"] == "await_started"), None)
    h.report.add(
        "V6-pause",
        "工具登记等待后 run 停成 waiting_external，等待事件带双截止点",
        expected="status=waiting_external、await_broker.status=waiting、"
        "await_started 含 await_id + 等待 deadline + run_deadline_at",
        actual=f"status={paused['status']}、await={row['status'] if row else None}、"
        f"首次登记={holder['created']}、事件={'有' if started else '缺'}",
        passed=(
            paused["status"] == "waiting_external"
            and holder["created"] is True
            and row is not None
            and row["status"] == "waiting"
            and row["tool_name"] == "accept.echo"
            and started is not None
            and started["payload"].get("await_id") == await_id
            and started["payload"].get("deadline_at")
            and started["payload"].get("run_deadline_at")
        ),
        evidence=[f"等待时长预算 deadline_at={row['deadline_at'] if row else None}"],
    )

    active_at_pause = paused["active_ms"]
    await asyncio.sleep(0.8)
    still = await h.fetch_run(run_id)
    h.report.add(
        "V6-wait-not-counted",
        "等待期间不烧时长、不涨工具调用（等待不计入 active_ms）",
        expected="0.8s 后 active_ms / budget_used 均不变",
        actual=f"active_ms {active_at_pause} → {still['active_ms']}、"
        f"budget_used 变={still['budget_used'] != paused['budget_used']}",
        passed=still["active_ms"] == active_at_pause
        and still["budget_used"] == paused["budget_used"],
    )

    resp = await h.callback(await_id, payload={"ok": True, "rows": 3})
    body = resp.json() if resp.status_code == 200 else {}
    after = await h.await_row(await_id)
    inbox = await h.inbox(run_id)
    h.report.add(
        "V6-callback",
        "回调落定：CAS waiting→granted 并投递唯一一次唤醒",
        expected="200、resumed=true、row=granted、attempts=1、inbox 恰好 1 条 resume",
        actual=f"{resp.status_code}、resumed={body.get('resumed')}、"
        f"status={after['status'] if after else None}、"
        f"attempts={after['attempts'] if after else None}、inbox={len(inbox)}",
        passed=(
            resp.status_code == 200
            and body.get("resumed") is True
            and after is not None
            and after["status"] == "granted"
            and after["attempts"] == 1
            and len(inbox) == 1
        ),
    )

    resume = await h.resume_payload(await_id)
    await h.rt._resume_run(run_id, resume)
    done = await h.fetch_run(run_id)
    h.report.add(
        "V6-resume",
        "唤醒后从检查点继续到终态，外部结果进结果信封",
        expected="status=done、result.outcome=success、text=外部结果已并入",
        actual=f"status={done['status']}、outcome={(done['result'] or {}).get('outcome')}、"
        f"text={(done['result'] or {}).get('text')!r}",
        passed=(
            done["status"] == "done"
            and (done["result"] or {}).get("text") == "外部结果已并入，任务完成"
        ),
    )


async def v7_await_idempotency(h: Harness) -> None:
    """同一笔外部请求的重放：登记幂等、回调幂等、鉴权拒绝。"""
    run_id = await h.seed_run(timeout_seconds=60)
    holder: dict[str, Any] = {}

    async def step_awaiting(g: _FakeGraph) -> dict[str, Any]:
        payload, created = await h.register_await(
            run_id, args={"q": "accept-v7"}, timeout_seconds=30
        )
        again, created_again = await h.register_await(
            run_id, args={"q": "accept-v7"}, timeout_seconds=30
        )
        holder.update(
            payload=payload, created=created, same=again["await_id"] == payload["await_id"]
        )
        holder["created_again"] = created_again
        g.snapshot = _Snapshot.awaiting(payload)
        return {"messages": [_Msg("ai", "等待回传")]}

    h.rt.graph = _FakeGraph([step_awaiting])
    await h.rt._process_run(run_id)
    await_id = holder["payload"]["await_id"]
    rows = await h.count("SELECT count(*) FROM await_broker WHERE run_id = :id", {"id": run_id})
    h.report.add(
        "V7-ensure-once",
        "同参重复登记复用同一行（幂等键 = 参数指纹）",
        expected="created=true → false、await_id 相同、该 run 只有 1 行等待",
        actual=f"首次={holder['created']}、再次={holder['created_again']}、"
        f"同 id={holder['same']}、行数={rows}",
        passed=(
            holder["created"] is True
            and holder["created_again"] is False
            and holder["same"] is True
            and rows == 1
        ),
    )

    first = await h.callback(await_id)
    replay = await h.callback(await_id)
    row = await h.await_row(await_id)
    inbox = await h.inbox(run_id)
    h.report.add(
        "V7-callback-replay",
        "回调重放只回既有状态，不产生第二次唤醒、不重复计数",
        expected="首次 resumed=true / 重放 resumed=false、attempts 仍为 1、inbox 仍 1 条",
        actual=f"首次={first.json().get('resumed')}、重放={replay.json().get('resumed')}、"
        f"attempts={row['attempts']}、inbox={len(inbox)}",
        passed=(
            first.status_code == 200
            and first.json().get("resumed") is True
            and replay.status_code == 200
            and replay.json().get("resumed") is False
            and row["attempts"] == 1
            and len(inbox) == 1
        ),
    )

    bad_token = await h.callback(await_id, token="0" * 32)
    bad_key = await h.callback(await_id, idempotency_key="not-the-registered-key")
    no_api_key = await h.callback(await_id, api_key=None)
    ghost = await h.callback(str(uuid.uuid4()))
    h.report.add(
        "V7-callback-auth",
        "回调双重鉴权与存在性判定（token/key/契约）",
        expected="token 错=403、idempotency_key 不一致=409、缺 X-API-Key=401、无此行=404",
        actual=f"token={bad_token.status_code}、key={bad_key.status_code}、"
        f"无 APIKey={no_api_key.status_code}、幽灵 id={ghost.status_code}",
        passed=(
            bad_token.status_code == 403
            and bad_key.status_code == 409
            and no_api_key.status_code == 401
            and ghost.status_code == 404
        ),
    )


async def v8_await_timeout(h: Harness) -> None:
    """等待超时：到点翻 expired 并唤醒一次，恢复载荷带结构化错误码。"""
    run_id = await h.seed_run(timeout_seconds=60)
    holder: dict[str, Any] = {}

    async def step_awaiting(g: _FakeGraph) -> dict[str, Any]:
        payload, _created = await h.register_await(
            run_id, args={"q": "accept-v8"}, timeout_seconds=1
        )
        holder["payload"] = payload
        g.snapshot = _Snapshot.awaiting(payload)
        return {"messages": [_Msg("ai", "等待回传")]}

    h.rt.graph = _FakeGraph([step_awaiting])
    await h.rt._process_run(run_id)
    await_id = holder["payload"]["await_id"]
    row = await h.await_row(await_id)

    from app.modules.awaits import service as awaits_service

    deadline = row["deadline_at"]
    async with h.sf() as db:
        expired = await awaits_service.expire_due(db, now=deadline + timedelta(seconds=1))
        for r in expired:
            await awaits_service.enqueue_resume(db, r, status="expired")
        await db.commit()
    after = await h.await_row(await_id)
    inbox = await h.inbox(run_id)
    second = await h.count(
        "SELECT count(*) FROM await_broker WHERE id = :id AND status = 'waiting'",
        {"id": await_id},
    )
    h.report.add(
        "V8-expire",
        "等待到期 CAS 成 expired，错误码结构化，唤醒恰好一次",
        expected="status=expired、error.code=await_expired、inbox 1 条、无残留 waiting",
        actual=f"status={after['status']}、code={(after['error'] or {}).get('code')}、"
        f"inbox={len(inbox)}、仍 waiting={second}",
        passed=(
            after["status"] == "expired"
            and (after["error"] or {}).get("code") == "await_expired"
            and len(inbox) == 1
            and second == 0
        ),
        evidence=[f"巡检周期 await_sweep_interval_seconds={_sweep_interval()}"],
    )

    payload = await h.resume_payload(await_id)
    h.report.add(
        "V8-resume-payload",
        "超时唤醒的恢复载荷如实回传 expired（模型看到结构化失败而非伪成功）",
        expected="status=expired、error.code=await_expired、waited_ms>0",
        actual=f"status={payload.get('status')}、"
        f"code={(payload.get('error') or {}).get('code')}、waited_ms={payload.get('waited_ms')}",
        passed=(
            payload.get("status") == "expired"
            and (payload.get("error") or {}).get("code") == "await_expired"
            and (payload.get("waited_ms") or 0) > 0
        ),
    )

    callback_after_expire = await h.callback(await_id)
    h.report.add(
        "V8-late-callback",
        "迟到回调不改状态、不二次唤醒（超时先到即终局）",
        expected="200 且 resumed=false、status 仍 expired、inbox 仍 1 条",
        actual=f"{callback_after_expire.status_code}、"
        f"resumed={callback_after_expire.json().get('resumed')}、"
        f"status={(await h.await_row(await_id))['status']}、inbox={len(await h.inbox(run_id))}",
        passed=(
            callback_after_expire.status_code == 200
            and callback_after_expire.json().get("resumed") is False
            and (await h.await_row(await_id))["status"] == "expired"
            and len(await h.inbox(run_id)) == 1
        ),
    )


async def v9_restart_safety(h: Harness) -> None:
    """重启对账：running 收殓为 failed，等待中的 run 不被误杀。"""
    waiting_run = await h.seed_run(timeout_seconds=60)
    holder: dict[str, Any] = {}

    async def step_awaiting(g: _FakeGraph) -> dict[str, Any]:
        payload, _created = await h.register_await(
            waiting_run, args={"q": "accept-v9"}, timeout_seconds=30
        )
        holder["await_id"] = payload["await_id"]
        g.snapshot = _Snapshot.awaiting(payload)
        return {"messages": [_Msg("ai", "等待回传")]}

    h.rt.graph = _FakeGraph([step_awaiting])
    await h.rt._process_run(waiting_run)
    zombie = await h.seed_run(status="running", timeout_seconds=60)

    foreign = await h.count(
        "SELECT count(*) FROM runs WHERE status = 'running' AND id <> :id", {"id": zombie}
    )
    if foreign:
        h.report.add(
            "V9-restart",
            "重启对账不改动非本轮的 running run",
            expected="库中存在其它 running run 时脚本不动全局状态（由单测覆盖）",
            actual=f"其它 running run = {foreign} 条，已跳过全局对账",
            passed=False,
            warn_only=True,
        )
        return

    await h.rt._reconcile_orphans()
    survivor = await h.fetch_run(waiting_run)
    dead = await h.fetch_run(zombie)
    h.report.add(
        "V9-restart",
        "重启对账只收殓 running，waiting_external 存活",
        expected="等待中的 run 仍 waiting_external；僵尸 run=failed + code=process_restart",
        actual=f"等待中={survivor['status']}、僵尸={dead['status']}、"
        f"code={(dead['error'] or {}).get('code')}",
        passed=(
            survivor["status"] == "waiting_external"
            and dead["status"] == "failed"
            and (dead["error"] or {}).get("code") == "process_restart"
        ),
    )


def _sweep_interval() -> Any:
    from app.core.config import settings

    return settings.await_sweep_interval_seconds


# ---------- 失败路径演练（P0-3 / V5：外部不可用不假完成）----------


def _dead_endpoint() -> str:
    """拿一个确定没人监听的地址：绑到 0 端口后立刻释放，此后再连必被拒。"""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def _node(h: Harness, name: str) -> Any:
    """真图节点的可调用体：跳过 LLM，但节点函数/工具/DB/钩子全是生产代码。

    编译会把手写节点包成 `PregelNode`（取不到原函数），故构建期临时让
    `StateGraph.compile` 返回自身——这与 `tests/` 里的做法一致，只影响本次构建。
    """
    from langgraph.graph.state import StateGraph

    from app.modules.engine import graph as graph_mod

    original = StateGraph.compile
    StateGraph.compile = lambda self, *a, **kw: self  # type: ignore[method-assign]
    try:
        builder = graph_mod.build_graph(h.rt)
    finally:
        StateGraph.compile = original
    return builder.nodes[name].runnable.afunc


def _node_config(h: Harness, run_id: str) -> dict[str, Any]:
    return {"configurable": {"run_id": run_id, "agent_id": h.agent_id}}


def _tool_state(
    name: str,
    *,
    builtin: str,
    risk: str,
    args: dict[str, Any] | None = None,
    budget_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage

    call_args = dict(args or {})
    return {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": name, "args": call_args, "id": "c1"}])
        ],
        "capability_cache": {
            "tools": [{"name": name, "kind": "builtin", "builtin": builtin, "risk_level": risk}]
        },
        "budget_state": dict(budget_state or {}),
    }


async def p0_3_failure_drills(h: Harness) -> None:
    """外部不可达：派发失败、连续熔断、run 收尾三段都用生产代码真跑。

    另两条失败路径（回调重复、等待到期）已在 V7 / V8 真库验过，此处不重复。
    """
    from app.core.config import settings
    from app.modules.engine.hooks import BudgetExceededError, RunContext, ToolFailureLoopError

    run_id = await h.seed_run(timeout_seconds=60)
    boom_run = await h.seed_run(timeout_seconds=60)
    loop_run = await h.seed_run(timeout_seconds=60)
    for rid in (run_id, boom_run, loop_run):
        # 生产里由 runtime 装配执行上下文；直接调节点须自己补上（钩子要 agent_id）
        h.rt._run_ctx[rid] = RunContext(rid, None, h.agent_id)

    dead = _dead_endpoint()
    original_base = settings.procurement_agent_base_url
    original_flag = settings.await_external_enabled
    settings.procurement_agent_base_url = dead
    settings.await_external_enabled = True  # 等待模式才是这条路径的生产前提
    try:
        await_gate = _node(h, "await_gate")
        out = await await_gate(
            {
                "pending_awaits": [
                    {
                        "id": "c1",
                        "name": "procurement_trigger",
                        "args": {"text": "accept-v16 失败演练"},
                        "builtin": "procurement_trigger",
                        "capability_id": None,
                        "risk_level": "write",
                    }
                ]
            },
            _node_config(h, run_id),
        )
        envelope = json.loads(str(out["messages"][0].content))
        err = envelope.get("error") or {}
        row = await h._one(
            "SELECT id, status, error, payload_out FROM await_broker WHERE run_id = :id",
            {"id": run_id},
        )
        artifacts = await h.count(
            "SELECT count(*) FROM run_artifacts WHERE run_id = :id", {"id": run_id}
        )
        h.report.add(
            "P0-3-dispatch-unreachable",
            "派发目标不可达：结构化失败进上下文 + 撤销等待行（不留悬挂、不伪成功）",
            expected="ok=false、code=external_unavailable、source=external、retryable=true、"
            "pending_awaits 清空、等待行=cancelled、无产物、载荷是失败信封（非自然语言结果）",
            actual=f"ok={envelope.get('ok')}、code={err.get('code')}、source={err.get('source')}、"
            f"retryable={err.get('retryable')}、rest={out.get('pending_awaits')}、"
            f"等待行={row and row['status']}、产物={artifacts}",
            passed=(
                envelope.get("ok") is False
                and envelope.get("tool") == "procurement_trigger"
                and "hint" in envelope
                and err.get("code") == "external_unavailable"
                and err.get("source") == "external"
                and err.get("retryable") is True
                and out.get("pending_awaits") == []
                and row is not None
                and row["status"] == "cancelled"
                and artifacts == 0
            ),
            evidence=[f"procurement_agent_base_url={dead}（已释放端口，连接必被拒）"],
        )

        tools_node = _node(h, "tools")
        # 参数逐次不同：真实模型重试时会调整入参，相同参数属于死循环闸的领地（见下一条）
        envelopes: list[dict[str, Any]] = []
        streaks: list[int] = []
        raised: ToolFailureLoopError | None = None
        carried: dict[str, Any] = {}
        for i in range(settings.tool_failure_limit):
            state = _tool_state(
                "procurement_status",
                builtin="procurement_status",
                risk="read",
                args={"task_id": f"accept-v16-{i}"},
                budget_state=carried,
            )
            try:
                step_out = await tools_node(state, _node_config(h, boom_run))
            except ToolFailureLoopError as e:
                raised = e
                break
            envelopes.append(json.loads(str(step_out["messages"][0].content)))
            carried = step_out["budget_state"]
            streaks.append(int(carried.get("tool_failure_streak") or 0))
        limit = settings.tool_failure_limit
        h.report.add(
            "P0-3-failure-streak",
            "连续外部失败达阈值：前两次是可累积的失败信封，第 N 次触发熔断",
            expected=f"streak={list(range(1, limit))}、信封 code=external_unavailable、第 {limit} 次抛 "
            f"ToolFailureLoopError（阈值 settings.tool_failure_limit={limit}）",
            actual=f"streak={streaks}、信封码={[e.get('error', {}).get('code') for e in envelopes]}、"
            f"熔断={'是' if raised else '否'}"
            f"（code={raised.code if raised else None}、tools={raised.tools if raised else None}）",
            passed=(
                streaks == list(range(1, limit))
                and all(e.get("ok") is False for e in envelopes)
                and all(e.get("error", {}).get("code") == "external_unavailable" for e in envelopes)
                and raised is not None
                and raised.code == "external_unavailable"
                and raised.source == "external"
                and raised.tools == ["procurement_status"] * limit
            ),
            evidence=[f"真实 builtin=procurement_status → {dead}"],
        )

        # 闸序实测：同参连续调用先被 loop-detect 钩子拦下（预算闸），到不了连续失败熔断
        loop_raised: BaseException | None = None
        for _ in range(limit):
            state = _tool_state(
                "procurement_status",
                builtin="procurement_status",
                risk="read",
                args={"task_id": "accept-v16-loop"},
            )
            try:
                await tools_node(state, _node_config(h, loop_run))
            except BaseException as e:  # noqa: BLE001 —— 要看清是哪一道闸先开
                loop_raised = e
                break
        loop_row = await h.fetch_run(loop_run)
        loop_artifacts = await h.count(
            "SELECT count(*) FROM run_artifacts WHERE run_id = :id", {"id": loop_run}
        )
        h.report.add(
            "P0-3-gate-precedence",
            "闸序：同参连打先撞死循环闸（loop_detect），异参才走连续失败熔断",
            expected="抛 BudgetExceededError(gate=loop_detect)、run 未判 done、无产物",
            actual=f"{type(loop_raised).__name__}、gate={getattr(loop_raised, 'gate', None)}、"
            f"run={loop_row['status']}、产物={loop_artifacts}",
            passed=(
                isinstance(loop_raised, BudgetExceededError)
                and loop_raised.gate == "loop_detect"
                and loop_row["status"] not in {"done", "failed"}
                and loop_artifacts == 0
            ),
        )

        async def step_boom(_g: _FakeGraph) -> dict[str, Any]:
            inner: dict[str, Any] = {}
            budget: dict[str, Any] = {}
            for i in range(limit):
                inner = _tool_state(
                    "procurement_status",
                    builtin="procurement_status",
                    risk="read",
                    args={"task_id": f"accept-v16-boom-{i}"},
                    budget_state=budget,
                )
                # 真实节点真调工具：不是"手工造一个熔断异常"，阈值判定也在生产代码里
                budget = (await tools_node(inner, _node_config(h, boom_run)))["budget_state"]
            raise AssertionError("连续失败未触发熔断")

        h.rt.graph = _FakeGraph([step_boom])
        await h.rt._process_run(boom_run)
        boom = await h.fetch_run(boom_run)
        result = boom["result"] or {}
        error = boom["error"] or {}
        waiting = await h.count(
            "SELECT count(*) FROM await_broker WHERE run_id = :id AND status = 'waiting'",
            {"id": boom_run},
        )
        h.report.add(
            "P0-3-failure-finalize",
            "熔断收尾：run=failed + 结构化错误码，失败也是一种结果（不发伪成功卡）",
            expected="status=failed、error.code=tool_failure_loop、failure_code=external_unavailable、"
            "结果信封 outcome=failed、无残留等待行",
            actual=f"status={boom['status']}、code={error.get('code')}、"
            f"failure_code={error.get('failure_code')}、outcome={result.get('outcome')}、"
            f"仍 waiting={waiting}",
            passed=(
                boom["status"] == "failed"
                and error.get("code") == "tool_failure_loop"
                and error.get("failure_code") == "external_unavailable"
                and result.get("outcome") == "failed"
                and waiting == 0
            ),
        )
    finally:
        settings.procurement_agent_base_url = original_base
        settings.await_external_enabled = original_flag


# ---------- 老 run 惰性补建（M7a）/ P1-9 / V11 / P1-4 / P2-6 ----------


async def m7a_legacy_task_backfill(h: Harness) -> None:
    """老 run（`input` 里没有 task_id）执行期惰性补建主任务并跑到终态。

    回归点：`backend.ensure_task_id` 曾经把 `uuid.UUID` 直接交给
    `tasks_service.ensure_task_for_conversation`（形参要 Conversation 对象，内部取
    `conversation.id`）→ `AttributeError` → run 直接 failed。这里复刻老 run 形态，
    断言补建真的落库且 run 不因补建而失败。
    """
    run_id = await h.seed_run(timeout_seconds=60, legacy_run=True)
    h.rt.graph = _FakeGraph([lambda _g: _done("老 run 补建任务后正常完成")])
    await h.rt._process_run(run_id)
    row = await h.fetch_run(run_id)
    tasks = await h.count(
        "SELECT count(*) FROM tasks WHERE conversation_id = :c",
        {"c": h.conversation_id},
    )
    h.report.add(
        "M7a-legacy-backfill",
        "无 task_id 的老 run 惰性补建主任务且不缺字段报错",
        expected="status=done、error 为空、会话下恰好 1 个主任务、结果信封非空",
        actual=f"status={row['status']}、error={row['error']}、tasks={tasks}、"
        f"result 非空={bool(row['result'])}",
        passed=(
            row["status"] == "done"
            and row["error"] is None
            and tasks == 1
            and (row["result"] or {}).get("text") == "老 run 补建任务后正常完成"
        ),
    )


async def p1_9_send_idempotent(h: Harness) -> None:
    """同一 `client_message_id` 提交两次（顺序重放 + 真并发）→ 只有一个 run。"""
    url = f"/api/v1/conversations/{h.conversation_id}/messages"
    seq_key = f"{_TAG}-seq-{uuid.uuid4().hex[:8]}"
    seq_text = f"{_TAG} 幂等探针（顺序重放）"
    body: dict[str, Any] = {
        "text": seq_text,
        "client_message_id": seq_key,
        "force_current_task": True,  # 绕开新任务检测（它在库里无 Worker 时会走 embedding）
    }
    first = await h.client.post(url, json=body)
    second = await h.client.post(url, json=body)
    run_a = h.track_run(first.json().get("run_id")) if first.status_code == 202 else ""
    run_b = h.track_run(second.json().get("run_id")) if second.status_code == 202 else ""
    runs = await h.count(
        "SELECT count(*) FROM runs WHERE conversation_id = :c "
        "AND input ->> 'client_message_id' = :k",
        {"c": h.conversation_id, "k": seq_key},
    )
    msgs = await h.count(
        "SELECT count(*) FROM messages WHERE conversation_id = :c AND content ->> 'text' = :t",
        {"c": h.conversation_id, "t": seq_text},
    )
    inbox = await h.count(
        "SELECT count(*) FROM inbox_events WHERE target_run_id = :r AND event_type = 'user_input'",
        {"r": run_a or str(uuid.uuid4())},
    )
    h.report.add(
        "P1-9-replay",
        "同键二次提交返回首次 run，不重复落消息/不重复建 run",
        expected="两次都 202、run_id 相同、runs=1、messages=1、inbox 只 1 条 user_input",
        actual=f"{first.status_code}/{second.status_code}、同 id={run_a == run_b}、"
        f"runs={runs}、messages={msgs}、inbox={inbox}",
        passed=(
            first.status_code == 202
            and second.status_code == 202
            and bool(run_a)
            and run_a == run_b
            and runs == 1
            and msgs == 1
            and inbox == 1
        ),
    )

    con_key = f"{_TAG}-con-{uuid.uuid4().hex[:8]}"
    con_text = f"{_TAG} 幂等探针（并发提交）"
    concurrent: dict[str, Any] = {
        "text": con_text,
        "client_message_id": con_key,
        "force_current_task": True,
    }
    left, right = await asyncio.gather(
        h.client.post(url, json=concurrent), h.client.post(url, json=concurrent)
    )
    con_ids = []
    for resp in (left, right):
        if resp.status_code == 202:
            con_ids.append(h.track_run(resp.json().get("run_id")))
    con_runs = await h.count(
        "SELECT count(*) FROM runs WHERE conversation_id = :c "
        "AND input ->> 'client_message_id' = :k",
        {"c": h.conversation_id, "k": con_key},
    )
    con_msgs = await h.count(
        "SELECT count(*) FROM messages WHERE conversation_id = :c AND content ->> 'text' = :t",
        {"c": h.conversation_id, "t": con_text},
    )
    h.report.add(
        "P1-9-concurrent",
        "真并发同键提交：唯一索引兜竞态，两边拿到同一个 run",
        expected="两个 202 响应同 run_id、runs=1、messages=1（竞态走回滚重查而非 500）",
        actual=f"{left.status_code}/{right.status_code}、"
        f"两个 id 相同={len(con_ids) == 2 and con_ids[0] == con_ids[1]}、"
        f"runs={con_runs}、messages={con_msgs}",
        passed=(
            left.status_code == 202
            and right.status_code == 202
            and len(con_ids) == 2
            and con_ids[0] == con_ids[1]
            and con_runs == 1
            and con_msgs == 1
        ),
    )


async def v11_result_replay(h: Harness) -> None:
    """结果重放：`run_events` 的最后一张结果卡 == `runs.result`，产物可原样取回。"""
    long_text = f"{_TAG} 长结果：\n" + "细节段落 " * 900  # 超 artifact_inline_max_chars
    run_id = await h.seed_run(timeout_seconds=60)
    h.rt.graph = _FakeGraph([lambda _g: _done(long_text)])
    await h.rt._process_run(run_id)

    row = await h.fetch_run(run_id)
    envelope = row["result"] or {}
    events = await h.events(run_id)
    cards = [e for e in events if e["event_type"] == "card"]
    # card 事件载荷是卡外壳 {card_type, payload: 结果信封, artifact_id}，重放卡片即取该外壳
    last_card = (cards[-1]["payload"] if cards else {}) or {}
    api_events = (await h.client.get(f"/api/v1/runs/{run_id}/events")).json()
    api_run = (await h.client.get(f"/api/v1/runs/{run_id}")).json()
    api_arts = (await h.client.get(f"/api/v1/runs/{run_id}/artifacts")).json()
    api_card = (
        next((e for e in reversed(api_events) if e["event_type"] == "card"), {}).get("payload")
        or {}
    )
    refs = [a.get("id") for a in (envelope.get("artifacts") or [])]
    h.report.add(
        "V11-card-replay",
        "历史重放的事件卡与实时结果同源（card 载荷 == runs.result）",
        expected="最后一个 card 事件的 payload == runs.result（两条读路径一致）",
        actual=f"card 数={len(cards)}、库内卡指向实时信封="
        f"{last_card.get('payload') == envelope}、API 卡与库内卡逐字一致="
        f"{api_card == last_card}、"
        f"api run.result 一致={api_run.get('result') == envelope}",
        passed=(
            len(cards) >= 1
            and last_card.get("card_type") == "result"
            and last_card.get("payload") == envelope
            and api_card == last_card
            and api_run.get("result") == envelope
        ),
        evidence=[
            f"信封字段={sorted(envelope)}",
            f"卡外壳字段={sorted(last_card)}",
        ],
    )

    h.report.add(
        "V11-artifact-replay",
        "长结果外置为产物：卡片引用可定位，正文可按 id 原样取回",
        expected="信封含 artifacts 引用、text 只留引用行+预览、artifacts 接口取回原文",
        actual=f"artifacts 引用={len(refs)}、库内产物={len(api_arts)}、"
        f"card.artifact_id 命中={last_card.get('artifact_id') == (refs[0] if refs else None)}",
        passed=(
            len(refs) == 1
            and len(api_arts) == 1
            and str(api_arts[0]["id"]) == str(refs[0])
            and last_card.get("artifact_id") == refs[0]
            and f"id={refs[0]}" in str(envelope.get("text"))
        ),
    )

    detail = await h.client.get(f"/api/v1/artifacts/{refs[0]}") if refs else None
    body = detail.json() if detail is not None and detail.status_code == 200 else {}
    payload = body.get("payload")
    stored = payload.get("text") if isinstance(payload, dict) else payload
    h.report.add(
        "V11-artifact-content",
        "产物正文与原始终答逐字一致（重放不丢内容）",
        expected="GET /artifacts/{id} 的 payload 正文 == 原始长文本",
        actual=f"{detail.status_code if detail is not None else '未取'}、"
        f"payload 外层={type(payload).__name__}、"
        f"长度={len(stored or '')}/原始 {len(long_text)}、逐字相等={stored == long_text}",
        passed=isinstance(stored, str) and stored == long_text,
    )


@contextlib.asynccontextmanager
async def _probe_worker(name: str, *, inputs: list[dict[str, Any]]) -> AsyncIterator[Any]:
    """临时 Worker 文件包：走 registry 真写入 `data/workers/`，用完删除。"""
    from app.modules.workers import registry

    try:
        registry.create_worker(
            name, description="验收探针（accept_v16 自建，跑完删除）", inputs=inputs
        )
        yield registry.get_def(name)
    finally:
        with contextlib.suppress(Exception):
            registry.delete_worker(name)


_GATE_INPUTS: list[dict[str, Any]] = [
    {
        "name": "quote_file",
        "type": "file",
        "required": True,
        "description": "待比价的报价单",
        "example": "报价单-2026Q1.xlsx",
    },
    {"name": "budget", "type": "number", "required": False, "description": "预算上限"},
]


async def p1_4_input_gate(h: Harness) -> None:
    """输入门：必需输入缺失 → 图与模型都不调用；齐备 → 契约进固定区并正常执行。"""
    name = f"{_TAG}-gate-{uuid.uuid4().hex[:6]}"
    async with _probe_worker(name, inputs=_GATE_INPUTS):
        miss_run = await h.seed_run(timeout_seconds=60, worker_name=name)
        miss_graph = _FakeGraph([lambda _g: _done("不该被调用")])
        h.rt.graph = miss_graph
        await h.rt._process_run(miss_run)
        row = await h.fetch_run(miss_run)
        error = row["error"] or {}
        events = await h.events(miss_run)
        has_error_event = any(e["event_type"] == "error" for e in events)
        h.report.add(
            "P1-4-gate-blocks",
            "必需输入缺失：调用模型之前结构化失败，图零调用",
            expected="图调用=0、status=failed、code=missing_inputs、retryable=false、"
            "missing=[quote_file]、result 信封非空（P0-5 不留 NULL）",
            actual=f"图调用={miss_graph.calls}、status={row['status']}、code={error.get('code')}、"
            f"retryable={error.get('retryable')}、missing={error.get('missing')}、"
            f"phase={error.get('phase')}、outcome={(row['result'] or {}).get('outcome')}",
            passed=(
                miss_graph.calls == 0
                and row["status"] == "failed"
                and error.get("code") == "missing_inputs"
                and error.get("retryable") is False
                and error.get("phase") == "pre_invoke"
                and list(error.get("missing") or []) == ["quote_file"]
                and (row["result"] or {}).get("outcome") == "failed"
                and bool(row["result"])
                and has_error_event
            ),
            evidence=[f"错误文案={str(error.get('detail'))[:60]}"],
        )

        ok_run = await h.seed_run(
            timeout_seconds=60,
            worker_name=name,
            inputs={"quote_file": "报价单-2026Q1.xlsx"},
        )
        ok_graph = _FakeGraph([lambda _g: _done("输入齐备，正常执行")])
        h.rt.graph = ok_graph
        await h.rt._process_run(ok_run)
        ok_row = await h.fetch_run(ok_run)
        configurable = (
            ((ok_graph.last_config or {}).get("configurable") or {}) if ok_graph.last_config else {}
        )
        contract = str(configurable.get("input_contract") or "")
        h.report.add(
            "P1-4-gate-passes",
            "必需输入齐备：契约与实测取值进固定区，图正常执行到终态",
            expected="图调用=1、status=done、input_contract 含实测取值",
            actual=f"图调用={ok_graph.calls}、status={ok_row['status']}、"
            f"契约命中={('报价单-2026Q1.xlsx' in contract)}、契约长度={len(contract)}",
            passed=(
                ok_graph.calls == 1
                and ok_row["status"] == "done"
                and "quote_file" in contract
                and "报价单-2026Q1.xlsx" in contract
            ),
        )


def _ops_view_sql() -> list[str]:
    """从 `docs/运维手册.md` §4B 现场解析三段视图 SQL（文档是验收对象，不复制副本）。"""
    doc = Path(__file__).resolve().parents[2] / "docs" / "运维手册.md"
    text = doc.read_text(encoding="utf-8")
    section = text.split("### 4B.")[1].split("## 5.")[0]
    return [block.strip() for block in re.findall(r"```sql\n(.*?)```", section, re.S)]


async def _run_view(h: Harness, sql: str) -> list[dict[str, Any]]:
    async with h.sf() as db:
        rows = (await db.execute(h._text(sql))).mappings().all()
    return [dict(r) for r in rows]


async def _explain(h: Harness, sql: str) -> list[str]:
    async with h.sf() as db:
        rows = (await db.execute(h._text(f"EXPLAIN {sql}"))).scalars().all()
    return [str(r) for r in rows]


async def p2_6_observability_views(h: Harness) -> None:
    """P2-6 三视图：文档原文 SQL 在真库可执行，且本轮探针数据在视图里可见。"""
    views = _ops_view_sql()
    if len(views) != 3:
        h.report.add(
            "P2-6-sql-blocks",
            "运维手册 §4B 应有 3 段视图 SQL",
            expected="解析到 3 段 ```sql 代码块（视图 7/8/9）",
            actual=f"解析到 {len(views)} 段",
            passed=False,
        )
        return

    # 探针 A：完成的 run（进视图 7 的 metrics 口径）
    done_run = await h.seed_run(timeout_seconds=60)
    h.rt.graph = _FakeGraph([lambda _g: _done("P2-6 视图探针：完成")])
    await h.rt._process_run(done_run)
    done_row = await h.fetch_run(done_run)
    metrics = (done_row["result"] or {}).get("metrics") or {}

    # 探针 B：超时等待（进视图 8 的 expired 分组）
    tool_name = f"{_TAG}.view-probe"
    wait_run = await h.seed_run(timeout_seconds=60)
    holder: dict[str, Any] = {}

    async def step_awaiting(g: _FakeGraph) -> dict[str, Any]:
        payload, _created = await h.register_await(
            wait_run, tool_name=tool_name, args={"q": "accept-p2-6"}, timeout_seconds=1
        )
        holder["payload"] = payload
        g.snapshot = _Snapshot.awaiting(payload)
        return {"messages": [_Msg("ai", "等待回传")]}

    h.rt.graph = _FakeGraph([step_awaiting])
    await h.rt._process_run(wait_run)
    from app.modules.awaits import service as awaits_service

    row = await h.await_row(holder["payload"]["await_id"])
    async with h.sf() as db:
        for expired in await awaits_service.expire_due(
            db, now=row["deadline_at"] + timedelta(seconds=1)
        ):
            await awaits_service.enqueue_resume(db, expired, status="expired")
        await db.commit()

    # 探针 C：输入门失败 run（进视图 9 的 code/phase 口径）
    probe_name = f"{_TAG}-view-{uuid.uuid4().hex[:6]}"
    async with _probe_worker(probe_name, inputs=_GATE_INPUTS):
        fail_run = await h.seed_run(timeout_seconds=60, worker_name=probe_name)
        h.rt.graph = _FakeGraph([lambda _g: _done("不该被调用")])
        await h.rt._process_run(fail_run)

    try:
        view7, view8, view9 = [await _run_view(h, sql) for sql in views]
    except Exception as exc:  # noqa: BLE001 —— 真库执行失败即文档交付不合格
        h.report.add(
            "P2-6-views-execute",
            "运维手册 §4B 三段 SQL 在真实 PostgreSQL 上可执行",
            expected="三段 SQL 原文直跑全部返回结果集",
            actual=f"{type(exc).__name__}: {exc}",
            passed=False,
        )
        return

    today = datetime.now(UTC).date()
    today_runs = await h.count(
        "SELECT count(*) FROM runs WHERE result -> 'metrics' ->> 'elapsed_ms' IS NOT NULL "
        "AND started_at > now() - interval '14 days' "
        "AND date_trunc('day', started_at)::date = :d",
        {"d": today},
    )
    view7_rows = {r["day"]: r for r in view7}
    today_row = view7_rows.get(today)
    h.report.add(
        "P2-6-views-execute",
        "三段 SQL 原文在真库可执行，视图 7 的天分桶与库内计数一致",
        expected="三段都返回结果集；视图 7 今日 runs 数 == 库内该窗口 run 数；"
        "active_p50 ≤ elapsed_p50（口径不颠倒）",
        actual=f"视图 7 行={len(view7)}、视图 8 行={len(view8)}、视图 9 行={len(view9)}、"
        f"今日分桶={today_row['runs'] if today_row else '缺'}、库内计数={today_runs}",
        passed=(
            today_row is not None
            and int(today_row["runs"]) == today_runs
            and today_runs >= 1
            and int(today_row["active_p50_ms"]) <= int(today_row["elapsed_p50_ms"])
        ),
        evidence=[f"本轮完成 run 的 metrics={metrics}"],
    )

    probe_await = next(
        (r for r in view8 if r["tool_name"] == tool_name and r["status"] == "expired"), None
    )
    h.report.add(
        "P2-6-view8-await",
        "视图 8 看得到本轮等待：expired 分组、overdue 归零、等待时长可读",
        expected="探针 tool_name 落在 status=expired 分组；该行 overdue=0、waited_avg_s ≥ 0",
        actual=f"视图 8 列={sorted(view8[0]) if view8 else '无行'}、"
        f"探针行={'有' if probe_await else '缺'}"
        + (
            f"（awaits={probe_await['awaits']}、overdue={probe_await['overdue']}、"
            f"waited_avg_s={probe_await['waited_avg_s']}、due_within_1h="
            f"{probe_await['due_within_1h']}）"
            if probe_await
            else ""
        ),
        passed=(
            probe_await is not None
            and int(probe_await["overdue"]) == 0
            and int(probe_await["due_within_1h"]) == 0  # 已 expired，不再计入"即将到期"
            and int(probe_await["waited_avg_s"]) >= 0
        ),
    )

    fail_probe = next(
        (r for r in view9 if r["code"] == "missing_inputs" and r["day"] == today), None
    )
    h.report.add(
        "P2-6-view9-failure",
        "视图 9 看得到 run 级失败：code × phase 分桶、retryable 如实",
        expected="今日存在 code=missing_inputs 且 phase=pre_invoke 的分桶，any_retryable=false",
        actual=f"视图 9 行={len(view9)}、探针桶={'有' if fail_probe else '缺'}"
        + (
            f"（runs={fail_probe['runs']}、phase={fail_probe['phase']}、"
            f"any_retryable={fail_probe['any_retryable']}）"
            if fail_probe
            else ""
        ),
        passed=(
            fail_probe is not None
            and int(fail_probe["runs"]) >= 1
            and fail_probe["phase"] == "pre_invoke"
            and fail_probe["any_retryable"] is False
        ),
    )

    plan7 = await _explain(h, views[0])
    plan9 = await _explain(h, views[2])
    seq7 = next((ln.strip() for ln in plan7 if "Seq Scan on runs" in ln), "")
    seq9 = next((ln.strip() for ln in plan9 if "Seq Scan on runs" in ln), "")
    h.report.add(
        "P2-6-index-hint",
        "文档「先 EXPLAIN 确认走索引」在真库的结论：时间列无索引",
        expected="视图 7/9 的时间窗口谓词命中 runs 的时间列索引",
        actual=(
            "视图 7/9 均为全表扫描：runs 表只有 "
            "ix_runs_agent_id / ix_runs_status / ix_runs_deadline_at / "
            "uq_runs_client_message_id，没有 started_at / finished_at 索引"
            if seq7 or seq9
            else "视图 7/9 未出现 runs 全表扫描"
        ),
        passed=not (seq7 or seq9),
        warn_only=True,
        evidence=[
            f"视图 7 计划首行={plan7[0].strip() if plan7 else '空'}",
            f"视图 9 计划首行={plan9[0].strip() if plan9 else '空'}",
            f"7: {seq7 or '未出现 Seq Scan'}",
            f"9: {seq9 or '未出现 Seq Scan'}",
        ],
    )


# ---------- P1-5 推送式进度 / P1-6 受阻支线收敛 ----------


async def _read_stream(
    h: Harness, run_id: str, *, after: int = 0
) -> tuple[list[dict[str, Any]], float]:
    """读真 SSE 流并解析 `id/event/data` 帧，返回（帧列表, 整条流耗时）。

    ASGITransport 会把响应体收齐才交付，所以量到的是"流跑完"的耗时——恰好能区分
    「NOTIFY 实时推送」与「25s 兜底轮询」：前者秒级收流，后者至少要等一次心跳超时。
    """
    frames: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    t0 = time.monotonic()
    async with h.client.stream("GET", f"/api/v1/runs/{run_id}/stream?after={after}") as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"SSE 返回 {resp.status_code}")
        async for line in resp.aiter_lines():
            if line.startswith("id: "):
                cur["seq"] = int(line[4:])
            elif line.startswith("event: "):
                cur["event"] = line[7:]
            elif line.startswith("data: "):
                cur["payload"] = json.loads(line[6:])
            elif not line and cur:
                frames.append(cur)
                cur = {}
    return frames, time.monotonic() - t0


async def p1_5_progress(h: Harness) -> None:
    """推送式进度（P1-5）：有变化才推、等待态换文案、终态退场、SSE 实流可续传。

    进度真源是主任务的反范式列（`recompute_progress` 唯一写入口），且四次推送都发生在
    **模型不在场**的时候（run 停在 waiting_external、子任务被前台收敛）——这正是
    "进度由平台推送、前端不必轮询模型"的可验证形态。
    """
    conv = await h.new_conversation()
    run_id = await h.seed_run(conversation_id=conv, timeout_seconds=60)
    task_id = await h.task_id(conv)
    active = await h.seed_step(task_id, seq=1, name="核对报价单", status="doing", kind="main")
    await h.seed_step(task_id, seq=2, name="生成比价结论", status="pending", kind="main")
    prog = await h.refresh_progress(task_id)
    await h.rt._set_run_status(run_id, "running")
    h.rt._progress_seen.pop(run_id, None)

    # 首帧 → 无变化重扫 → 子任务收口（模型不在场）
    await h.rt._sweep_progress()
    first = await h.events_of(run_id, "progress")
    await h.rt._sweep_progress()
    again = await h.events_of(run_id, "progress")
    await h.step_status(active, "done")
    await h.rt._sweep_progress()
    moved = await h.events_of(run_id, "progress")
    moved_row = await h.task_row(task_id)

    head = first[0]["payload"] if first else {}
    h.report.add(
        "P1-5-progress-dedupe",
        "进度推送：首帧 label 取活跃子任务名、无变化不重发、子任务收口后 done 跟上",
        expected="首帧 exactly {done:0,total:2,label:'核对报价单'}；重复巡检帧数不变；"
        "子任务 done 后新帧 done=1，且与任务卡反范式列同数",
        actual=f"首帧={head}、重复巡检后帧数={len(again)}、"
        f"收口后帧数={len(moved)}、末帧={moved[-1]['payload'] if moved else '缺'}、"
        f"任务卡 done/total={moved_row['progress_done']}/{moved_row['progress_total']}、"
        f"seq={[f['seq'] for f in moved]}",
        passed=(
            head == {"done": 0, "total": 2, "label": "核对报价单"}
            and prog["total"] == 2
            and len(first) == 1
            and len(again) == 1
            and len(moved) == 2
            and moved[-1]["payload"] == {"done": 1, "total": 2, "label": ""}
            and [f["seq"] for f in moved] == sorted({f["seq"] for f in moved})
        ),
    )

    # 等待联动：run 进/出 waiting_external，快照 label 跟着换（同一 run，无步骤变化）
    await h.rt._set_run_status(run_id, "waiting_external")
    await h.rt._sweep_progress()
    waiting = await h.events_of(run_id, "progress")
    await h.rt._set_run_status(run_id, "running")
    await h.rt._sweep_progress()
    resumed = await h.events_of(run_id, "progress")
    h.report.add(
        "P1-5-progress-awaiting",
        "等待联动：run 进入 waiting_external 时进度文案换成「等待外部回调」，离开后还原",
        expected="waiting_external 帧 label='等待外部回调'（done 不变）；"
        "回到 running 后再推一帧，label 不再是等待文案",
        actual=f"等待帧={waiting[-1]['payload'] if waiting else '缺'}、"
        f"恢复帧={resumed[-1]['payload'] if resumed else '缺'}、"
        f"帧数 {len(moved)} → {len(waiting)} → {len(resumed)}",
        passed=(
            len(waiting) == 3
            and waiting[-1]["payload"] == {"done": 1, "total": 2, "label": "等待外部回调"}
            and len(resumed) == 4
            and resumed[-1]["payload"]["label"] != "等待外部回调"
            and resumed[-1]["payload"]["done"] == 1
        ),
    )

    # 终态退场：终态 run 不再进快照，内存记账随之清理；同会话/异会话在跑 run 都不受牵连
    fin_conv = await h.new_conversation()
    fin_run = await h.seed_run(conversation_id=fin_conv, timeout_seconds=60)
    fin_task = await h.task_id(fin_conv)
    await h.seed_step(fin_task, seq=1, name="产出结论", status="doing", kind="main")
    await h.refresh_progress(fin_task)
    await h.rt._sweep_progress()
    fin_frames = await h.events_of(fin_run, "progress")
    fin_seen = fin_run in h.rt._progress_seen
    before_run_frames = len(await h.events_of(run_id, "progress"))
    h.rt.graph = _FakeGraph([lambda _g: _done("进度探针：终态退场")])
    await h.rt._process_run(fin_run)
    await h.rt._sweep_progress()
    after_run_frames = await h.events_of(run_id, "progress")
    fin_plan = await h.events_of(fin_run, "plan_updated")
    fin_row = await h.task_row(fin_task)
    h.report.add(
        "P1-5-progress-terminal",
        "终态退场：终态 run 从进度快照与内存记账里消失，且不牵连在跑的 run",
        expected="未终态时该 run 有 progress 帧（pending 优先取阶段表文案「排队中」）并被记账；"
        "终态 + 巡检后记账清空；另一会话在跑 run 的帧数不因该 run 终态而增加",
        actual=f"终态前记账={fin_seen}、其 progress 帧={[f['payload'] for f in fin_frames]}、"
        f"终态后记账={fin_run in h.rt._progress_seen}、"
        f"在跑 run 帧数 {before_run_frames} → {len(after_run_frames)}、"
        f"终态 run 所在任务={fin_row['status']}/{fin_row['progress_done']}、"
        f"plan_updated={len(fin_plan)} 条",
        passed=(
            fin_seen
            and [f["payload"] for f in fin_frames] == [{"done": 0, "total": 1, "label": "排队中"}]
            and fin_run not in h.rt._progress_seen
            and len(after_run_frames) == before_run_frames
            and len(fin_plan) >= 1
        ),
        evidence=[
            "终态 run 所在会话的任务回写（主线推进、进度变化）改由 plan_updated 事件表达："
            "该会话已无在跑 run，进度不再有可推的载体",
        ],
    )

    # SSE 实流：历史补齐 + 实时推送 + 终态收流（真 asyncpg LISTEN，非兜底轮询）
    sse_run = await h.seed_run(conversation_id=conv, timeout_seconds=60)

    async def slow_done(_g: _FakeGraph) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return _done("进度探针：SSE 终答")

    h.rt.graph = _FakeGraph([slow_done])

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(0.15)
            await h.rt._sweep_progress()

    sweeper = asyncio.create_task(sweep_loop())
    stream_task = asyncio.create_task(_read_stream(h, sse_run))
    frames: list[dict[str, Any]] = []
    elapsed = 0.0
    error = ""
    try:
        await asyncio.sleep(0.4)  # 让流先接上：历史补齐 + LISTEN 就位
        await h.rt._process_run(sse_run)  # 实时事件 → NOTIFY → 推帧 → 终态收流
        frames, elapsed = await asyncio.wait_for(stream_task, timeout=10)
    except Exception as exc:  # noqa: BLE001 —— 收不到流就是验收失败，如实记录
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
        if not stream_task.done():
            stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stream_task

    db_events = await h.events(sse_run)
    expected = [
        {"seq": e["seq"], "event": e["event_type"], "payload": e["payload"]} for e in db_events
    ]
    last = frames[-1] if frames else {}
    h.report.add(
        "P1-5-sse-live",
        "SSE 实流：历史与实时事件逐帧与库内一致、含进度帧、终态收流且不是兜底轮询",
        expected="无异常；帧序列 == 库内事件序列（逐字）；含 progress 帧；"
        "末帧 run_status=done；收流耗时 < 10s（心跳兜底是 25s）",
        actual=f"错误={error or '无'}、帧数={len(frames)}、库内事件={len(db_events)}、"
        f"事件类型={[f['event'] for f in frames]}、末帧={last.get('payload')}、"
        f"耗时={elapsed:.2f}s",
        passed=(
            not error
            and frames == expected
            and any(f["event"] == "progress" for f in frames)
            and last.get("event") == "run_status"
            and last.get("payload", {}).get("status") == "done"
            and elapsed < 10
        ),
    )

    # 断线续传：带 after 游标重放，应只补最后一帧
    tail, _tail_elapsed = await _read_stream(h, sse_run, after=int(frames[-2]["seq"]))
    h.report.add(
        "P1-5-sse-resume",
        "SSE 断线续传：带 after 游标重连只补游标之后的事件（终态 run 补完即收流）",
        expected=f"after={frames[-2]['seq'] if len(frames) >= 2 else '?'} 时恰好收到 1 帧，"
        "且与全程最后一帧逐字一致",
        actual=f"补到 {len(tail)} 帧：{[f['event'] for f in tail]}",
        passed=len(frames) >= 2 and tail == [frames[-1]],
    )


async def _converge_probe(h: Harness) -> tuple[str, str, str]:
    """造一个"干净"的收敛现场：新会话 + 主线已收口 + 一条受阻支线（绑在 run 上）。

    返回 (run_id, task_id, blocked_step_id)。主线全 done 是有意的：这样 close 会触发
    自动收口、requeue 才需要 `autoclose=False` 兜住，`_CONVERGE_TARGET` 的语义差异
    才真的可观测。
    """
    conv = await h.new_conversation()
    run_id = await h.seed_run(conversation_id=conv, timeout_seconds=60)
    task_id = await h.task_id(conv)
    for seq, name in ((1, "收集需求"), (2, "产出结论")):
        await h.seed_step(task_id, seq=seq, name=name, status="done", kind="main")
    step = await h.seed_step(
        task_id, seq=3, name="等待用户确认口径", status="blocked", kind="branch", run_id=run_id
    )
    await h.refresh_progress(task_id)
    return run_id, task_id, step


def _converge_path(task_id: str, step_id: str) -> str:
    return f"/api/v1/tasks/{task_id}/steps/{step_id}/converge"


async def _converge(
    h: Harness, task_id: str, step_id: str, action: str, *, detail: str | None = None
) -> Any:
    body: dict[str, Any] = {"action": action}
    if detail is not None:
        body["detail"] = detail
    return await h.client.post(_converge_path(task_id, step_id), json=body)


async def p1_6_converge(h: Harness) -> None:
    """受阻支线收敛端点（P1-6）：动作语义、事件留痕、事务边界、无 run 归属的降级。"""
    # —— close：可让主任务收口 ——
    run_id, task_id, blocked = await _converge_probe(h)
    resp = await _converge(h, task_id, blocked, "close", detail="用户已另行确认口径")
    step = await h.step_row(blocked)
    task = await h.task_row(task_id)
    events = await h.events(run_id)
    resolution = step["resolution"] or {}
    h.report.add(
        "P1-6-converge-close",
        "收敛 close：支线 skipped + resolution 枚举化 + 事件 unblocked + 主线随之收口",
        expected="HTTP 200；status=skipped、resolved_at 非空、resolution.action=close、"
        "reason=manual、detail 落在 detail 字段；run 事件恰好 +1 条 unblocked；"
        "主线已全收口 → 任务自动 done、进度 3/3",
        actual=f"HTTP={resp.status_code}、status={step['status']}、"
        f"resolved_at={'有' if step['resolved_at'] else '无'}、resolution={resolution}、"
        f"事件={[(e['event_type'], e['payload']) for e in events]}、"
        f"任务={task['status']}、进度={task['progress_done']}/{task['progress_total']}",
        passed=(
            resp.status_code == 200
            and step["status"] == "skipped"
            and step["resolved_at"] is not None
            and resolution.get("action") == "close"
            and resolution.get("reason") == "manual"
            and resolution.get("detail") == "用户已另行确认口径"
            and resolution.get("converged_at")
            and [e["event_type"] for e in events] == ["unblocked"]
            and events[0]["payload"]["status"] == "skipped"
            and events[0]["payload"]["reason"] == "manual"
            and events[0]["payload"]["step_id"] == blocked
            and task["status"] == "done"
            and task["finished_at"] is not None
            and (task["progress_done"], task["progress_total"]) == (3, 3)
        ),
    )

    # —— requeue：重新排队不顺手判完成（autoclose=False）——
    run2, task2, blocked2 = await _converge_probe(h)
    resp = await _converge(h, task2, blocked2, "requeue")
    step = await h.step_row(blocked2)
    task = await h.task_row(task2)
    events = await h.events(run2)
    h.report.add(
        "P1-6-converge-requeue",
        "收敛 requeue：支线回 pending、resolved_at 清空、且不顺手把主任务判完成",
        expected="HTTP 200；status=pending、resolved_at 为空、resolution.action=requeue、"
        "无 escalated；事件 unblocked；主线已全 done 且无未决支线（其余收口条件都满足），"
        "任务仍 active、进度 2/3",
        actual=f"HTTP={resp.status_code}、status={step['status']}、"
        f"resolved_at={'有' if step['resolved_at'] else '无'}、resolution={step['resolution']}、"
        f"事件={[e['event_type'] for e in events]}、任务={task['status']}、"
        f"进度={task['progress_done']}/{task['progress_total']}",
        passed=(
            resp.status_code == 200
            and step["status"] == "pending"
            and step["resolved_at"] is None
            and (step["resolution"] or {}).get("action") == "requeue"
            and not (step["resolution"] or {}).get("escalated")
            and [e["event_type"] for e in events] == ["unblocked"]
            and task["status"] == "active"
            and (task["progress_done"], task["progress_total"]) == (2, 3)
        ),
    )

    # 409 + 回滚：对已不在 blocked 的支线再收敛，响应 409 且库里零残留
    before_events = len(await h.events(run2))
    resp = await _converge(h, task2, blocked2, "close", detail="不该落库")
    after_step = await h.step_row(blocked2)
    after_events = await h.events(run2)
    h.report.add(
        "P1-6-converge-conflict",
        "收敛冲突：非受阻支线返回 409，事务回滚不留残留（状态/事件都不动）",
        expected="HTTP 409 且 detail 指明仅受阻可收敛；支线仍 pending、resolution 仍是 "
        "requeue 那份、run 事件数不变",
        actual=f"HTTP={resp.status_code}、detail={resp.json().get('detail') if resp.headers.get('content-type', '').startswith('application/json') else resp.text[:80]}、"
        f"status={after_step['status']}、resolution={(after_step['resolution'] or {}).get('action')}、"
        f"事件数 {before_events} → {len(after_events)}",
        passed=(
            resp.status_code == 409
            and "blocked" in str(resp.json().get("detail"))
            and after_step["status"] == "pending"
            and (after_step["resolution"] or {}).get("detail") is None
            and len(after_events) == before_events
        ),
    )

    # —— escalate：保持受阻 + 标记已接手 + 事件是 blocked（不是 unblocked）——
    run3, task3, blocked3 = await _converge_probe(h)
    orphan = await h.seed_step(task3, seq=4, name="人工补记的支线", status="blocked", kind="branch")
    resp = await _converge(h, task3, blocked3, "escalate", detail="转交人工值班")
    step = await h.step_row(blocked3)
    task = await h.task_row(task3)
    events = await h.events(run3)
    resolution = step["resolution"] or {}
    h.report.add(
        "P1-6-converge-escalate",
        "收敛 escalate：支线保持受阻并标记 escalated，事件为 blocked（语义没被抹平）",
        expected="HTTP 200；status 仍 blocked、resolution.escalated=true、action=escalate；"
        "run 事件恰好 1 条且类型为 blocked；任务仍 active（受阻是闸门）",
        actual=f"HTTP={resp.status_code}、status={step['status']}、resolution={resolution}、"
        f"事件={[e['event_type'] for e in events]}、任务={task['status']}",
        passed=(
            resp.status_code == 200
            and step["status"] == "blocked"
            and resolution.get("action") == "escalate"
            and resolution.get("escalated") is True
            and [e["event_type"] for e in events] == ["blocked"]
            and task["status"] == "active"
        ),
    )

    # 无 run 归属的支线：只落状态，不发事件（没有可附着的事件流）
    before = len(await h.events(run3))
    resp = await _converge(h, task3, orphan, "close")
    orphan_row = await h.step_row(orphan)
    after = await h.events(run3)
    h.report.add(
        "P1-6-converge-orphan",
        "无 run 归属的支线可收敛：状态落库但不凭空造事件流",
        expected="HTTP 200；status=skipped、resolution.action=close；该会话 run 事件数不变",
        actual=f"HTTP={resp.status_code}、status={orphan_row['status']}、"
        f"resolution.action={(orphan_row['resolution'] or {}).get('action')}、"
        f"事件数 {before} → {len(after)}",
        passed=(
            resp.status_code == 200
            and orphan_row["status"] == "skipped"
            and (orphan_row["resolution"] or {}).get("action") == "close"
            and len(after) == before
        ),
    )

    # 404：不属于该任务的支线
    others = await h.new_conversation()
    other_task = await h.task_id(others)
    resp = await _converge(h, other_task, orphan, "close")
    h.report.add(
        "P1-6-converge-404",
        "收敛越权：支线不属于该任务时 404（不做跨任务写）",
        expected="HTTP 404 且 detail=子任务不存在",
        actual=f"HTTP={resp.status_code}、detail={resp.json().get('detail')}",
        passed=resp.status_code == 404 and resp.json().get("detail") == "子任务不存在",
    )


# ---------- 分组 mcp：真实 MCP 对端对账（P2-1 / P2-4）----------

_PROBE_SERVER = Path(__file__).resolve().parent / "mcp_probe_server.py"


@contextlib.asynccontextmanager
async def _local_listener() -> AsyncIterator[str]:
    """起一个真实监听套接字（随机端口），让外部子进程能按 URL 真取件。

    `lifespan="off"` 是刻意的：只要 HTTP 路由，不要 mcp 健康循环 / 收件箱循环这些进程级
    后台任务——否则验收自己会多出一套并发循环，搅乱其它分组的时窗。
    """
    import uvicorn

    from app.core.config import settings
    from app.main import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve(), name="accept-v16-listener")
    try:
        for _ in range(200):
            if server.started or task.done():
                break
            await asyncio.sleep(0.05)
        if not server.started:
            raise RuntimeError("本地监听未就绪（uvicorn 未 started）")
        port = server.servers[0].sockets[0].getsockname()[1]
        previous = settings.platform_base_url
        settings.platform_base_url = f"http://127.0.0.1:{port}"
        try:
            yield settings.platform_base_url
        finally:
            settings.platform_base_url = previous
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=15)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


async def _connect_probe_server(h: Harness) -> tuple[str, str, dict[str, Any]]:
    """登记探针能力并接进池，返回 (cap_id, name, payload)。

    刻意只连这一个 Server，不走 `rebuild()` 全库重连：库里真实存在的 `filesystem`(stdio) /
    `websearch`(http) 与本条对账无关，不该因为验收被动拉起。落库走 `_sync_tools`
    （rebuild 的同一条路径），所以 `capability_tools` 行是真的。
    """
    from app.modules.capabilities.mcp_client import McpConnection, mcp_pool
    from app.modules.capabilities.models import Capability

    cap_uuid = uuid.uuid4()
    name = f"{_TAG}-probe-{cap_uuid.hex[:6]}"
    payload = {"transport": "stdio", "command": sys.executable, "args": [str(_PROBE_SERVER)]}
    async with h.sf() as db:
        db.add(
            Capability(
                id=cap_uuid,
                type="mcp",
                category="external",
                name=name,
                risk_level="write",
                description="accept-v16 真实 MCP 对端（本地最小 Server）",
                payload=payload,
                enabled=True,
            )
        )
        await db.commit()
    h.capability_ids.append(str(cap_uuid))

    conn = McpConnection(cap_uuid, name, payload)
    await conn.connect()
    await mcp_pool._sync_tools(cap_uuid, await conn.health_probe())
    # `_conns` 是池的私有映射：探针**故意**插进去，好让 list_enabled_tools / call_tool
    # 走与生产完全相同的代码路径，而不是自己另开一条捷径
    mcp_pool._conns[cap_uuid] = conn
    h.mcp_conns.append(cap_uuid)
    return str(cap_uuid), name, payload


async def _seed_probe_file(h: Harness, filename: str, content: bytes, mime: str) -> str:
    """落一个真实文件（`files` 行 + 磁盘字节）；`sha256` 唯一，内容带随机尾巴防撞。"""
    import hashlib

    from app.core.config import settings
    from app.modules.files.models import File

    rel = Path("files") / _TAG / f"{uuid.uuid4().hex}{Path(filename).suffix}"
    abs_path = settings.data_dir / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)
    h.probe_paths.append(abs_path)

    file_uuid = uuid.uuid4()
    async with h.sf() as db:
        db.add(
            File(
                id=file_uuid,
                path=str(rel),
                filename=filename,
                mime=mime,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
        await db.commit()
    h.file_ids.append(str(file_uuid))
    return str(file_uuid)


async def p2_1_mcp_real(h: Harness) -> None:
    """真实 MCP 对端对账：注解→风险级、`_meta` 下发面、真 socket 取件闭环（P2-1 / P2-4）。

    平台与对端之间没有一分 stub：对端是真实 stdio 子进程（真实 list_tools 注解、真实
    tools/call `_meta`），取件是真实 TCP 往返（uvicorn 监听套接字），凭据是平台真实签发的
    `callback_token`。
    """
    import hashlib

    from sqlalchemy import text

    from app.core.config import settings
    from app.modules.awaits import service as awaits_service
    from app.modules.capabilities.mcp_client import mcp_pool
    from app.modules.discovery.assembler import expand_capability
    from app.modules.engine import tools_builtin
    from app.modules.engine.tool_context import ToolContext
    from app.modules.runs.models import RunArtifact

    cap_id, cap_name, payload = await _connect_probe_server(h)
    cap_uuid = uuid.UUID(cap_id)  # 池内映射以 UUID 为键，字符串查不到（graph.py 同样转换）
    cap: dict[str, Any] = {
        "id": cap_id,
        "name": cap_name,
        "type": "mcp",
        "risk_level": "write",
        "payload": payload,
    }
    prefix = f"mcp__{cap_name}__"

    # -- P2-4：官方注解 → 工具级风险级 --

    tools = {t["tool_name"]: t for t in await expand_capability(cap, "pinned")}
    risks = {name: t["risk_level"] for name, t in tools.items()}
    names_ok = bool(tools) and all(t["name"] == f"{prefix}{name}" for name, t in tools.items())
    h.report.add(
        "P2-4-mcp-annotations",
        "真实 MCP Server 的官方注解细化到工具级风险级（readOnly / destructive / 缺省不猜）",
        expected=(
            "quote_lookup=read（readOnlyHint）、quote_purge=dangerous（destructiveHint）、"
            "quote_submit=write（无注解即沿用能力级，不按规范默认值推导）、"
            "暴露名 mcp__{server}__{tool}"
        ),
        actual=f"风险级={json.dumps(risks, ensure_ascii=False, sort_keys=True)}、暴露名合规={names_ok}",
        evidence=[f"capability_tools 由真实 list_tools 落库，共 {len(tools)} 个工具"],
        passed=(
            risks.get("quote_lookup") == "read"
            and risks.get("quote_purge") == "dangerous"
            and risks.get("quote_submit") == "write"
            and names_ok
        ),
    )

    hardened = {
        t["tool_name"]: t["risk_level"]
        for t in await expand_capability({**cap, "risk_level": "dangerous"}, "pinned")
    }
    declared = {
        t["tool_name"]: t["risk_level"]
        for t in await expand_capability(
            {**cap, "payload": {**payload, "tool_risk_levels": {"quote_submit": "read"}}}, "pinned"
        )
    }
    h.report.add(
        "P2-4-mcp-risk-priority",
        "风险级优先级：管理员声明 > 能力级 dangerous（注解不可洗白）> 注解 > 能力级兜底",
        expected=(
            "能力级 dangerous 时：带 readOnlyHint 的 quote_lookup 与无注解的 quote_submit 都仍为 "
            "dangerous；另给 payload.tool_risk_levels.quote_submit=read → 声明生效为 read"
        ),
        actual=(
            f"能力级 dangerous={json.dumps(hardened, ensure_ascii=False, sort_keys=True)}、"
            f"声明覆盖={json.dumps(declared, ensure_ascii=False, sort_keys=True)}"
        ),
        passed=(
            hardened.get("quote_lookup") == "dangerous"
            and hardened.get("quote_submit") == "dangerous"
            and declared.get("quote_submit") == "read"
        ),
    )

    # -- P1-10：执行上下文真的到了对端 --

    run_id = await h.seed_run()
    task_id = await h.task_id()
    meta_ctx = ToolContext(
        run_id=run_id, task_id=task_id, step_id=None, idempotency_key=f"{_TAG}-meta"
    )
    echoed = json.loads(
        await mcp_pool.call_tool(
            cap_uuid, "quote_lookup", {"q": "accept-v16"}, meta=meta_ctx.as_meta()
        )
    )
    context = echoed.get("context") or {}
    h.report.add(
        "P1-10-mcp-meta",
        "平台下发的执行上下文原样落到真实 MCP Server 的 `_meta.agentos`",
        expected="对端读到 run_id / task_id / idempotency_key，与 ToolContext.as_meta() 逐键一致",
        actual=f"对端回显={json.dumps(context, ensure_ascii=False, sort_keys=True)}",
        evidence=[
            f"平台侧 meta={json.dumps(meta_ctx.as_meta(), ensure_ascii=False, sort_keys=True)}"
        ],
        passed=(
            context.get("run_id") == run_id
            and context.get("task_id") == task_id
            and context.get("idempotency_key") == f"{_TAG}-meta"
        ),
    )

    leaked = sorted(set(context) & {"callback_token", "await_id", "callback_url", "files_url"})
    h.report.add(
        "P2-1-meta-claims",
        "方案 §P2-1 称「files_url 已落进 MCP `_meta.agentos`」：实测该类凭据不进 MCP 通道",
        expected="外部 MCP Server 应能从 `_meta` 拿到取件凭据，否则无法自助取件",
        actual=(
            "代码里不存在该投递：MCP 调用点（graph.py 工具节点）构造的 ToolContext 不含任何等待"
            "凭据，`_meta` 只有身份字段；取件载荷只经 builtin 派发通道下发"
            "（ToolContext → _await_callback → args.await_callback），而等待路径仅对 "
            "is_dispatch_builtin 的 builtin 工具开放（MCP 工具永不入等待）"
        ),
        evidence=[
            f"对端 `_meta` 里的取件类键={leaked or '无'}",
            "结论：P2-1 取件通道当前只服务 builtin 等待工具；要么补 MCP 侧投递，"
            "要么把该段宣称收紧为「仅 builtin 通道」（本文档不可改，记为观察项）",
        ],
        passed=False,
        warn_only=True,
    )

    # -- P2-1：真实 socket 取件闭环 --

    interrupt, _ = await h.register_await(run_id, tool_name="procurement_trigger")
    await_id = interrupt["await_id"]
    token = awaits_service.make_callback_token(await_id)

    body = b"quote_id,price\n" + uuid.uuid4().hex.encode() + b"\n"
    artifact_id = await _seed_probe_file(h, "accept-v16-quote.csv", body, "text/csv")
    input_body = b"spec\n" + uuid.uuid4().hex.encode() + b"\n"
    input_id = await _seed_probe_file(h, "accept-v16-spec.txt", input_body, "text/plain")
    foreign_run = await h.seed_run()
    foreign_body = b"foreign\n" + uuid.uuid4().hex.encode() + b"\n"
    foreign_id = await _seed_probe_file(h, "accept-v16-foreign.csv", foreign_body, "text/csv")
    async with h.sf() as db:
        for fid, rid, size, name in (
            (artifact_id, run_id, len(body), "accept-v16-quote.csv"),
            (foreign_id, foreign_run, len(foreign_body), "accept-v16-foreign.csv"),
        ):
            db.add(
                RunArtifact(
                    run_id=uuid.UUID(rid),
                    kind="file",
                    name=name,
                    mime="text/csv",
                    size=size,
                    storage="file",
                    file_id=uuid.UUID(fid),
                )
            )
        await db.execute(
            text("UPDATE runs SET input = input || CAST(:patch AS jsonb) WHERE id = :id"),
            {"patch": json.dumps({"attachment_ids": [input_id]}), "id": run_id},
        )
        await db.commit()

    async with _local_listener() as base:
        # 派发载荷必须在监听起来之后现算：`files_url` 是按 platform_base_url 生成的
        pickup_ctx = ToolContext(
            run_id=run_id,
            task_id=task_id,
            step_id=None,
            idempotency_key=f"{_TAG}-pickup",
            callback_token=token,
            await_id=await_id,
            callback_url=awaits_service.callback_url(await_id),
            files_url=awaits_service.pickup_url_template(await_id),
        )
        captured: dict[str, Any] = {}

        async def _fake_procurement_request(
            method: str,
            path: str,
            *,
            json_body: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> str:
            """只替掉**平台之外**的采购 Agent（真第三方不在验收范围）；平台侧从
            ToolContext 到 args 的组装全程真实执行。"""
            captured.update({"method": method, "path": path, "body": json_body or {}})
            return "ok"

        original_request = tools_builtin._procurement_request
        tools_builtin._procurement_request = _fake_procurement_request
        try:
            await tools_builtin._procurement_trigger({"text": "accept-v16"}, pickup_ctx)
        finally:
            tools_builtin._procurement_request = original_request

        dispatch = (captured.get("body") or {}).get("await_callback") or {}
        h.report.add(
            "P2-1-dispatch-payload",
            "派发类 builtin 的下发载荷携带取件模板（files_url 与 await_id/callback_token 同源）",
            expected=f"body.await_callback.files_url = {base}/api/v1/open/files/{{file_id}}/content?await_id=…",
            actual=f"keys={sorted(dispatch)}、files_url={dispatch.get('files_url')}",
            evidence=[
                f"token 是否出现在 URL 里={token in str(dispatch.get('files_url'))}（凭据走 X-Callback-Token 头，不落访问日志）",
                f"下发（唯一被替掉的是采购 Agent 的 HTTP 出口）={json.dumps(dispatch, ensure_ascii=False)}",
            ],
            passed=(
                dispatch.get("files_url")
                == f"{base}/api/v1/open/files/{{file_id}}/content?await_id={await_id}"
                and dispatch.get("await_id") == await_id
                and dispatch.get("token") == token
            ),
        )

        resp = await h.client.get(
            f"/api/v1/open/awaits/{await_id}/files",
            headers={"X-API-Key": _OPEN_TOKEN, "X-Callback-Token": token},
        )
        items = resp.json() if resp.status_code == 200 else []
        listed = {i["file_id"] for i in items if isinstance(i, dict)}
        sources = sorted({i["source"] for i in items if isinstance(i, dict)})
        h.report.add(
            "P2-1-list-awaits",
            "取件清单按等待反查 run：产出（run_artifacts）+ 输入附件两条来源，范围外文件不出现",
            expected="HTTP 200、source={artifact,input}、含本 run 两件、不含他人 run 的文件",
            actual=(
                f"HTTP={resp.status_code}、source={sources}、"
                f"本 run 两件齐全={artifact_id in listed and input_id in listed}、"
                f"含他人 run 文件={foreign_id in listed}"
            ),
            evidence=[f"清单={json.dumps(items, ensure_ascii=False)[:400]}"],
            passed=(
                resp.status_code == 200
                and sources == ["artifact", "input"]
                and artifact_id in listed
                and input_id in listed
                and foreign_id not in listed
            ),
        )

        async def pickup(file_id: str, **overrides: Any) -> dict[str, Any]:
            args = {"await_callback": dispatch, "file_id": file_id, "api_key": _OPEN_TOKEN}
            args.update(overrides)
            return json.loads(await mcp_pool.call_tool(cap_uuid, "file_pickup", args))

        async def deny(
            no: str,
            title: str,
            *,
            want: int,
            expected: str,
            file_id: str | None = None,
            **overrides: Any,
        ) -> None:
            res = await pickup(file_id or artifact_id, **overrides)
            h.report.add(
                no,
                title,
                expected=expected,
                actual=f"HTTP={res.get('status')}、detail={res.get('detail')}",
                passed=res.get("status") == want,
            )

        got = await pickup(artifact_id)
        h.report.add(
            "P2-1-pickup-200",
            "外部 MCP Server 经真实 socket 取件成功（字节 / 摘要 / 响应头逐项对齐）",
            expected="HTTP 200、字节数与 sha256 与源文件一致、X-Content-Type-Options=nosniff、附件下载语义",
            actual=(
                f"HTTP={got.get('status')}、bytes={got.get('bytes')}/{len(body)}、"
                f"sha256 一致={got.get('sha256') == hashlib.sha256(body).hexdigest()}、"
                f"nosniff={got.get('nosniff')}、disposition={got.get('content_disposition')}"
            ),
            evidence=[
                "实取地址="
                + str(dispatch.get("files_url") or "").replace("{file_id}", artifact_id),
                f"地址基址={base}（真实监听套接字，非 ASGITransport）",
                f"派发载荷键={sorted(dispatch)}（凭据同源：本次等待的 callback_token）",
            ],
            passed=(
                got.get("status") == 200
                and got.get("bytes") == len(body)
                and got.get("sha256") == hashlib.sha256(body).hexdigest()
                and got.get("nosniff") == "nosniff"
                and "attachment" in str(got.get("content_disposition") or "")
            ),
        )

        await deny(
            "P2-1-pickup-401",
            "取件缺平台级 X-API-Key：401（平台级鉴权先于归属判定）",
            want=401,
            expected="HTTP 401",
            api_key="not-the-platform-token",
        )
        await deny(
            "P2-1-pickup-403-token",
            "取件 callback_token 不对：403（凭据错与「不存在」分开，便于调用方自纠）",
            want=403,
            expected="HTTP 403 且 detail=callback_token 无效",
            token_override="0" * 32,
        )
        await deny(
            "P2-1-pickup-403-missing",
            "取件不带 callback_token：403（平台级 Key 不足以取件）",
            want=403,
            expected="HTTP 403 且 detail=callback_token 无效（未带凭据与凭据错同码）",
            drop_token=True,
        )
        await deny(
            "P2-1-pickup-404-foreign",
            "取件他人 run 的文件：404（不给出「存在但无权」的存在性预言）",
            want=404,
            expected="HTTP 404",
            file_id=foreign_id,
        )
        await deny(
            "P2-1-pickup-404-unknown",
            "取件不存在的 file_id：404",
            want=404,
            expected="HTTP 404",
            file_id=str(uuid.uuid4()),
        )

        limit_before = settings.open_file_max_bytes
        settings.open_file_max_bytes = 8
        try:
            too_big = await pickup(artifact_id)
        finally:
            settings.open_file_max_bytes = limit_before
        h.report.add(
            "P2-1-pickup-413",
            "取件超出体积上限：413（上限取自 settings.open_file_max_bytes）",
            expected="HTTP 413（上限临时压到 8 字节，检查后还原）",
            actual=f"HTTP={too_big.get('status')}、detail={too_big.get('detail')}",
            evidence=[f"还原后上限={limit_before} 字节"],
            passed=too_big.get("status") == 413,
        )


# ---------- 容量预警（P0-2 增量：按目标 Agent 的 tool_budget 更早预警）----------


async def _seed_tool_capability(h: Harness, name: str) -> None:
    """落一个 `type=tool` 的真能力行（展开即 1 个工具，便于把工具数算准）。"""
    from app.modules.capabilities.models import Capability

    cap_id = uuid.uuid4()
    async with h.sf() as db:
        db.add(
            Capability(
                id=cap_id,
                type="tool",
                category="internal",
                name=name,
                risk_level="read",
                description="accept-v16 容量预警探针（跑完删除）",
                payload={
                    "schema": {
                        "type": "function",
                        "function": {"name": name, "description": "容量预警探针", "parameters": {}},
                    }
                },
                enabled=True,
            )
        )
        await db.commit()
    h.capability_ids.append(str(cap_id))


async def p0_2_tool_budget_warning(h: Harness) -> None:
    """发布/注册期按目标 Agent 的 `tool_budget` 出预警：只报警告，**不阻断**（方案 §4 P0-2）。

    自带 3 个真能力（展开 3 个工具）+ 一个 `tool_budget=2` 的真 Agent 行：
    平台能算出"这个 Agent 装不下 1 个工具"，但依赖关系由配置者掌握，故只提示不拒绝。
    """
    from app.modules.agents.models import Agent
    from app.modules.discovery.assembler import partition_tools
    from app.modules.workers import registry

    tag = uuid.uuid4().hex[:6]
    cap_names = [f"{_TAG}-budget-{tag}-{i}" for i in range(3)]
    for cap_name in cap_names:
        await _seed_tool_capability(h, cap_name)

    agent_name = f"{_TAG}-budget-agent-{tag}"
    unknown_name = f"{_TAG}-budget-ghost-{tag}"
    async with h.sf() as db:
        agent = Agent(name=agent_name, tool_budget=2)
        db.add(agent)
        await db.flush()
        h.extra_agent_ids.append(str(agent.id))
        await db.commit()

    worker = f"{_TAG}-budget-{tag}"
    api_worker = f"{_TAG}-budget-api-{tag}"
    created: list[str] = []
    try:
        registry.create_worker(
            worker, description="验收探针（accept_v16 自建，跑完删除）", capabilities=cap_names
        )
        created.append(worker)

        resp = await h.client.post(
            f"/api/v1/workers/{worker}/versions",
            json={"target_agents": [agent_name, unknown_name]},
        )
        data = resp.json() if resp.status_code < 400 else {}
        warnings = data.get("warnings") or []
        budget_hit = [w for w in warnings if "tool_budget=2" in w and agent_name in w]
        ghost_hit = [w for w in warnings if unknown_name in w]

        # 老调用方（不带请求体）→ 行为与升级前一致
        legacy = await h.client.post(f"/api/v1/workers/{worker}/versions")
        legacy_warnings = (legacy.json() if legacy.status_code < 400 else {}).get("warnings")

        # 预警里的算术必须与运行期分区口径一致：装不下的数量 == 真被 dropped 的数量
        dropped = partition_tools([], [{"name": n} for n in cap_names], tool_budget=2)["dropped"]

        h.report.add(
            "P0-2-budget-warning",
            "发布版本时按目标 Agent 的 tool_budget 出预警（含名字不存在），且不阻断发布",
            expected="HTTP 201、tool_count=3、warnings 含「tool_budget=2」的装不下提示 +"
            "「不存在」提示；不带 body 的老调用方 warnings 为空",
            actual=f"HTTP={resp.status_code}、tool_count={data.get('tool_count')}、"
            f"warnings={len(warnings)} 条、装不下提示={len(budget_hit)}、不存在提示={len(ghost_hit)}、"
            f"老调用方 HTTP={legacy.status_code}、warnings={legacy_warnings}",
            evidence=[f"提示原文：{budget_hit[0]}" if budget_hit else "无装不下提示"]
            + (["不存在提示原文：" + ghost_hit[0]] if ghost_hit else []),
            passed=(
                resp.status_code == 201
                and data.get("tool_count") == 3
                and len(budget_hit) == 1
                and len(ghost_hit) == 1
                and legacy.status_code == 201
                and legacy_warnings == []
            ),
        )

        h.report.add(
            "P0-2-budget-arithmetic",
            "预警文案的数量口径与运行期工具分区一致（说几个会 dropped 就真几个）",
            expected="partition_tools(tool_budget=2) 的 dropped 数 == 展开工具数 - 2 == 1，"
            "且提示里的数字与之相同",
            actual=f"真实 dropped={dropped}、提示数字对得上="
            f"{bool(budget_hit) and f'至少 {len(dropped)} 个工具' in budget_hit[0]}",
            passed=len(dropped) == data.get("tool_count", 0) - 2 == 1
            and bool(budget_hit)
            and f"至少 {len(dropped)} 个工具" in budget_hit[0],
        )

        # 开放 API 一键注册同样是"非阻断预警"
        reg = await h.client.post(
            "/api/v1/open/workers/register",
            json={
                "worker": {
                    "name": api_worker,
                    "description": "验收探针（accept_v16 自建，跑完删除）",
                    "capabilities": cap_names,
                    "playbook": "# 探针\n",
                },
                "capabilities": [],
                "target_agents": [agent_name, unknown_name],
            },
            headers={"X-API-Key": _OPEN_TOKEN},
        )
        reg_data = reg.json() if reg.status_code < 400 else {}
        created.append(api_worker)
        reg_warnings = reg_data.get("warnings") or []
        h.report.add(
            "P0-2-register-warning",
            "开放 API 一键注册按 target_agents 出同口径预警（非阻断，仍落盘）",
            expected="HTTP 201、action=created、warnings 含「tool_budget=2」与「不存在」",
            actual=f"HTTP={reg.status_code}、action={reg_data.get('action')}、"
            f"warnings={reg_warnings}",
            passed=(
                reg.status_code == 201
                and reg_data.get("action") == "created"
                and any("tool_budget=2" in w for w in reg_warnings)
                and any(unknown_name in w for w in reg_warnings)
            ),
        )
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                registry.delete_worker(name)


# ---------- 限流生效口径对账（P1-2 变更点 4：不做猜测性默认值）----------


async def p1_2_limits_resolution(h: Harness) -> None:
    """真库 provider 的 `limits` 现状与生效值对账。

    P1-2 变更点 4（"给 limits 写默认值随迁移下发"）复核后仍**不做**：开发库 9 个
    provider 里 8 个走本地网关，唯一直连云端的用的是按消费档位的动态限流——任何
    硬编码数值都是猜测（见 `ratelimit.py` 模块 docstring）。这里把结论固化成判据：
    库内不得出现猜测性写入，未配置时严格回落 settings 全局缺省。
    """
    from sqlalchemy import select

    from app.core.config import settings
    from app.modules.models_module.models import ModelProvider
    from app.modules.models_module.ratelimit import ProviderRateLimiter

    async with h.sf() as db:
        rows = (await db.execute(select(ModelProvider))).scalars().all()
    snapshot = [
        {
            "id": str(r.id),
            "via_gateway": bool((r.params or {}).get("via_gateway")),
            "limits": dict(r.limits or {}),
        }
        for r in rows
    ]
    probe = ProviderRateLimiter()
    for p in snapshot:
        probe.remember(p)
    effective = {p["id"]: probe.effective(p["id"]) for p in snapshot}
    global_default = {
        "rate_limit_rps": float(settings.model_default_rate_limit_rps),
        "rate_limit_tpm": float(settings.model_default_rate_limit_tpm),
    }
    unset = sum(1 for p in snapshot if not p["limits"])
    direct = sum(1 for p in snapshot if not p["via_gateway"])
    h.report.add(
        "P1-2-limits-resolution",
        "未配 limits 的 provider 严格回落全局缺省（无猜测性默认值写入）",
        expected="每个 provider 生效值 == settings 全局缺省（默认 0/0=不限）",
        actual=f"providers={len(snapshot)}、limits 为空={unset}、非网关={direct}、"
        f"全局缺省={global_default}、生效值集合={sorted({json.dumps(v, sort_keys=True) for v in effective.values()})}",
        passed=bool(snapshot) and all(v == global_default for v in effective.values()),
    )

    sample = snapshot[0]["id"] if snapshot else "missing"
    original = settings.model_default_rate_limit_rps
    try:
        settings.model_default_rate_limit_rps = 7.0
        probe.remember({"id": sample, "limits": {}})
        fallback = probe.effective(sample)["rate_limit_rps"]
        probe.remember({"id": sample, "limits": {"rate_limit_rps": 0}})
        escape = probe.effective(sample)["rate_limit_rps"]
    finally:
        settings.model_default_rate_limit_rps = original
    h.report.add(
        "P1-2-limits-priority",
        "生效优先级 provider.limits > settings 全局缺省，显式 0 为逃生舱",
        expected="未配 → 7.0（全局缺省）；显式 0 → 0.0（该 provider 关限流）",
        actual=f"fallback={fallback}、escape={escape}",
        passed=fallback == 7.0 and escape == 0.0,
    )


# ---------- 子任务级输入门（P1-4 收尾：门在 run 内部，不只是整个 run）----------

_STEP_REF = "拉取报价"
# 真图用例的输入文本：必须被判成「任务」（simple/complex 都可），否则图会走
# chitchat 直连 agent 的快速通道，压根不经过 input_gate。实测该文本稳定判为任务。
_STEP_GATE_TEXT = (
    f"{_TAG} 探针：请读取随消息提供的报价单文件，完成当前子任务「{_STEP_REF}」并给出比价结论。"
)
_STEP_INPUTS: list[dict[str, Any]] = [
    {
        "name": "quote_file",
        "type": "file",
        "required": True,
        "description": "待比价的报价单",
        "example": "报价单-2026Q1.xlsx",
    },
    {"name": "sla_hours", "type": "number", "required": False, "description": "响应时限（小时）"},
]

_MULTI_FILE_INPUTS: list[dict[str, Any]] = [
    {"name": "quote_a", "type": "file", "required": True, "description": "甲方报价单"},
    {"name": "quote_b", "type": "file", "required": True, "description": "乙方报价单"},
]


class _RecordingGraph:
    """真图外壳：记录每次 `ainvoke` 的 config，其余原样委派。

    `runtime._invoke_and_finalize` 每次进图都从 DB 重算子任务预检并随 configurable 下发，
    故"恢复后门还挂不挂"这件事实的权威来源就是这份 config——比事后推断图内状态更硬。
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.configs: list[Any] = []

    @property
    def calls(self) -> int:
        return len(self.configs)

    @property
    def last_gate(self) -> dict[str, Any] | None:
        if not self.configs:
            return None
        conf = (self.configs[-1] or {}).get("configurable") or {}
        return conf.get("step_input_contract")

    async def ainvoke(self, payload: Any, config: Any) -> dict[str, Any]:
        self.configs.append(config)
        return await self.inner.ainvoke(payload, config)

    async def aget_state(self, config: Any) -> Any:
        return await self.inner.aget_state(config)


@contextlib.asynccontextmanager
async def _real_graph(h: Harness, *, threads: list[str]) -> AsyncIterator[_RecordingGraph]:
    """真图 + 真 PG 检查点：子任务门要跨「暂停 → 恢复」存活，假图替不了这一段。

    用完即 `adelete_thread`：检查点在 PG 里按 thread_id 存留，不随会话级联删除。
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from app.modules.engine.graph import build_graph
    from app.modules.engine.runtime import _pg_dsn

    cm = AsyncPostgresSaver.from_conn_string(_pg_dsn())
    saver = await cm.__aenter__()
    await saver.setup()
    h.rt.saver = saver
    graph = _RecordingGraph(build_graph(h.rt))
    h.rt.graph = graph
    try:
        yield graph
    finally:
        for thread in threads:
            with contextlib.suppress(Exception):
                await saver.adelete_thread(thread)
        h.rt.saver = None
        h.rt.graph = None
        await cm.__aexit__(None, None, None)


@contextlib.asynccontextmanager
async def _step_gate_worker(
    name: str, *, inputs: list[dict[str, Any]] | None = None
) -> AsyncIterator[Any]:
    """临时 Worker：**run 级不声明输入**、子任务声明——门只能由子任务这一层触发。"""
    from app.modules.workers import registry

    try:
        registry.create_worker(name, description="验收探针（accept_v16 自建，跑完删除）")
        registry.create_sub_worker(
            name, "v1", _STEP_REF, kind="main", inputs=inputs or _STEP_INPUTS
        )
        yield registry.get_def(name)
    finally:
        with contextlib.suppress(Exception):
            registry.delete_worker(name)


async def _gate_run(h: Harness, worker: str, *, trigger: str = "manual") -> tuple[str, str, str]:
    """造一个「run 级齐备、当前子任务缺输入」的真 run；返回 (会话, run, 子任务)。"""
    conv = await h.new_conversation()
    run_id = await h.seed_run(
        conversation_id=conv,
        worker_name=worker,
        timeout_seconds=120,
        trigger=trigger,
        text=_STEP_GATE_TEXT,
    )
    task_id = await h.task_id(conv)
    step_id = await h.seed_step(
        task_id,
        seq=1,
        name=_STEP_REF,
        kind="main",
        status="doing",
        run_id=run_id,
        worker_step_ref=_STEP_REF,
    )
    return conv, run_id, step_id


def _gate_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    """最后一条 `confirmation_request` 的载荷（前端补输入卡就是照它长的）。"""
    rows = [e for e in events if e["event_type"] == "confirmation_request"]
    return dict(rows[-1]["payload"] or {}) if rows else {}


def _step_contract_text(state: Any) -> str:
    values = getattr(state, "values", None) or {}
    return str((values.get("protected_context") or {}).get("system_prompt") or "")


def _intent_of(state: Any) -> str:
    """图内实际判出的意图（`task`/`chitchat`）：chitchat 会绕过 input_gate，
    把它读进断言证据，失败时才能一眼分辨"门坏了"还是"文本被判成闲聊了"。"""
    values = getattr(state, "values", None) or {}
    return str((values.get("protected_context") or {}).get("intent") or "")


async def p1_4_step_gate(h: Harness) -> None:
    """子任务级输入门：当前子任务缺必需输入 → 真图 interrupt 等用户补，而不是判死整个 run。"""
    from app.modules.workers.preflight import STEP_CONTRACT_HEADER

    name = f"{_TAG}-stepgate-{uuid.uuid4().hex[:6]}"
    async with _step_gate_worker(name):
        conv, run_id, step_id = await _gate_run(h, name)
        async with _real_graph(h, threads=[conv]) as graph:
            await h.rt._process_run(run_id)
            row = await h.fetch_run(run_id)
            gate = _gate_event(await h.events(run_id))
            body = gate.get("payload") or {}
            snapshot = await graph.aget_state({"configurable": {"thread_id": conv}})
            stopped = tuple(getattr(snapshot, "next", ()) or ())
            intent = _intent_of(snapshot)
            h.report.add(
                "P1-4-step-gate-interrupt",
                "当前子任务缺必需输入：真图停在 input_gate，run 置等待确认（不是硬失败）",
                expected="status=paused_awaiting_confirm、confirmation_request.reason=input_required、"
                "检查点 next=('input_gate',)、run.error 为空、意图判为 task（chitchat 不经过此门）",
                actual=f"status={row['status']}、reason={gate.get('reason')}、next={stopped}、"
                f"intent={intent or '(未装配)'}、error={row['error']}、模型调用前挂起={graph.calls}",
                evidence=[f"子任务「{body.get('step_name')}」缺 {body.get('missing')}"],
                passed=(
                    row["status"] == "paused_awaiting_confirm"
                    and gate.get("reason") == "input_required"
                    and stopped == ("input_gate",)
                    and intent == "task"
                    and not row["error"]
                    and graph.calls == 1
                ),
            )

            declared = [item.get("name") for item in body.get("inputs") or []]
            quote = next((i for i in body.get("inputs") or [] if i.get("name") == "quote_file"), {})
            h.report.add(
                "P1-4-step-gate-payload",
                "挂起载荷可直接渲染补输入表单（定位 + 缺失清单 + 声明明细）",
                expected="payload 六件套齐全、step_id/sub_ref 指向真子任务、missing=['quote_file']、"
                "message 提到子任务名、inputs 明细含类型与 provided 标记",
                actual=f"step_id 对得上={body.get('step_id') == step_id}、"
                f"sub_ref={body.get('sub_ref')}、missing={body.get('missing')}、"
                f"demo={declared}、type={quote.get('type')}、required={quote.get('required')}、"
                f"provided={quote.get('provided')}、label={quote.get('label')}",
                passed=(
                    body.get("step_id") == step_id
                    and body.get("sub_ref") == _STEP_REF
                    and body.get("step_name") == _STEP_REF
                    and list(body.get("missing") or []) == ["quote_file"]
                    and _STEP_REF in str(body.get("message") or "")
                    and declared == ["quote_file", "sla_hours"]
                    and quote.get("type") == "file"
                    and quote.get("required") is True
                    and quote.get("provided") is False
                    and bool(quote.get("label"))
                    and quote.get("value") is None
                ),
            )

            bad = await h.client.post(
                f"/api/v1/runs/{run_id}/confirm", json={"inputs": {"NotAnInput": "x"}}
            )
            # `answer=""` + inputs：前端补输入卡的原样请求体（只提交取值、不带答复）
            good = await h.client.post(
                f"/api/v1/runs/{run_id}/confirm",
                json={"answer": "", "inputs": {"quote_file": "报价单-2026Q1.xlsx"}},
            )
            pending = [r for r in await h.inbox(run_id) if r["status"] == "new"]
            inbox_payload = dict(pending[0]["payload"] or {}) if pending else {}
            h.report.add(
                "P1-4-step-gate-confirm-http",
                "补输入提交：非法取值 422 挡在 API 外，合法取值投 confirmation 事件并带 inputs",
                expected="非法输入名 → HTTP 422；合法（answer 为空串、只带 inputs，同前端补输入卡）"
                " → 202/status=resuming；收件箱恰好 1 条 new，"
                "载荷 inputs={'quote_file': '报价单-2026Q1.xlsx'}",
                actual=f"非法 HTTP={bad.status_code}、合法 HTTP={good.status_code}、"
                f"detail={str(bad.json().get('detail'))[:30] if bad.status_code == 422 else ''}、"
                f"new 条数={len(pending)}、载荷 inputs={inbox_payload.get('inputs')}、"
                f"answer={inbox_payload.get('answer')!r}",
                passed=(
                    bad.status_code == 422
                    and good.status_code == 202
                    and good.json().get("status") == "resuming"
                    and len(pending) == 1
                    and pending[0]["event_type"] == "confirmation"
                    and inbox_payload.get("inputs") == {"quote_file": "报价单-2026Q1.xlsx"}
                ),
            )

            event = await h.claim_next_for(run_id)
            resume_gate: dict[str, Any] = {}
            resume_body: dict[str, Any] = {}
            prompt = ""
            stopped_after = ("(未恢复)",)
            after: dict[str, Any] = {"status": row["status"]}
            hung: list[dict[str, Any]] = []
            if event is None:
                # 门没挂起来就不会有待认领事件：如实报告，别拿空事件去喂 _handle
                h.report.add(
                    "P1-4-step-gate-resume",
                    "补输入后从暂停点恢复：取值落库 → 门重判为齐备 → 契约进固定区且只一份",
                    expected="收件箱存在待认领的 confirmation 事件（门先得挂起来）",
                    actual="收件箱无 new 事件：门未挂起，补输入无处投递",
                    passed=False,
                )
            else:
                claimed = dict(event)
                await h.rt._handle(claimed)
                running = h.rt._run_tasks.get(run_id)
                if running is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(running, timeout=180)
                after = await h.fetch_run(run_id)
                stored = ((await h.run_input(run_id)).get("inputs") or {}).get("quote_file")
                resume_gate = graph.last_gate or {}
                resume_body = resume_gate.get("payload") or {}
                resumed_state = await graph.aget_state({"configurable": {"thread_id": conv}})
                prompt = _step_contract_text(resumed_state)
                stopped_after = tuple(getattr(resumed_state, "next", ()) or ())
                hung = [
                    e
                    for e in await h.events(run_id)
                    if e["event_type"] == "confirmation_request"
                    and (e["payload"] or {}).get("reason") == "input_required"
                ]
                # 恢复后走到哪儿取决于分类：simple 直达 agent 收尾，complex 先进 planner →
                # confirm_plan（计划人审，manual 触发会再挂一次）。判据只压"跨过了这道门"，
                # 不把后续路径的差异当成失败。
                h.report.add(
                    "P1-4-step-gate-resume",
                    "补输入后从暂停点恢复：取值落库 → 门重判为齐备 → 契约进固定区且只一份",
                    expected="run.input.inputs 含提交值；恢复那次 configurable 的 step_input_contract"
                    ".ok=True 且 missing=[]；固定区含契约头且仅 1 份；检查点不再停在 input_gate、"
                    "input_required 只挂过 1 次（不重复挂）",
                    actual=f"认领事件={claimed.get('event_type')}、落库取值={stored}、"
                    f"恢复后 ok={resume_gate.get('ok')}、missing={resume_body.get('missing')}、"
                    f"契约头份数={prompt.count(STEP_CONTRACT_HEADER)}、"
                    f"input_required 挂起次数={len(hung)}、"
                    f"恢复后检查点 next={stopped_after}、run 状态={after['status']}",
                    evidence=[f"契约片段={resume_gate.get('contract', '')[:80]!r}"]
                    if resume_gate
                    else [],
                    passed=(
                        claimed.get("event_type") == "confirmation"
                        and stored == "报价单-2026Q1.xlsx"
                        and resume_gate.get("ok") is True
                        and list(resume_body.get("missing") or []) == []
                        and "报价单-2026Q1.xlsx" in str(resume_gate.get("contract") or "")
                        and prompt.count(STEP_CONTRACT_HEADER) == 1
                        and len(hung) == 1
                        and stopped_after != ("input_gate",)
                    ),
                )

        # timer 触发（无人值守）：缺输入不挂起，也不注入契约——对齐 ADR-16 的计划确认策略
        conv_t, run_t, _ = await _gate_run(h, name, trigger="timer")
        async with _real_graph(h, threads=[conv_t]) as graph_t:
            await h.rt._process_run(run_t)
            row_t = await h.fetch_run(run_t)
            events_t = await h.events(run_t)
            gate_t = _gate_event(events_t)
            state_t = await graph_t.aget_state({"configurable": {"thread_id": conv_t}})
            prompt_t = _step_contract_text(state_t)
            intent_t = _intent_of(state_t)
            stopped_t = tuple(getattr(state_t, "next", ()) or ())
            timer_gate = graph_t.last_gate or {}
            # 只扫 input_required 这一类挂起：run 若因**模型自己**调 ask_user 而暂停
            # （reason=subtask_clarification）不算门挂起——那是 agent 的澄清支线，不是这道门。
            hung_t = [
                e
                for e in events_t
                if e["event_type"] == "confirmation_request"
                and (e["payload"] or {}).get("reason") == "input_required"
            ]
            h.report.add(
                "P1-4-step-gate-timer",
                "timer 触发时缺输入不挂起（无人值守等不到人）：不注入契约、不发补输入卡",
                expected="全程无 reason=input_required 的 confirmation_request、固定区不含契约头、"
                "预检本身仍然如实判「缺」（不是靠放过判定蒙混）、且确实走了任务路径"
                "（`context_assembly` 已装配 ⇒ 拓扑上唯一的出边必然经过 input_gate）；"
                "run 不因此门停住（后续若被模型自己的澄清支线挂住属另一回事）",
                actual=f"status={row_t['status']}、intent={intent_t or '(未装配)'}、"
                f"门 reason={gate_t.get('reason') or '(无挂起)'}、input_required 次数={len(hung_t)}、"
                f"预检 ok={timer_gate.get('ok')}、missing={((timer_gate.get('payload') or {}).get('missing'))}、"
                f"契约头份数={prompt_t.count(STEP_CONTRACT_HEADER)}、检查点 next={stopped_t}",
                passed=(
                    intent_t == "task"
                    and not hung_t
                    and timer_gate.get("ok") is False
                    and list((timer_gate.get("payload") or {}).get("missing") or [])
                    == ["quote_file"]
                    and STEP_CONTRACT_HEADER not in prompt_t
                    and stopped_t != ("input_gate",)
                ),
            )


async def p1_4_multi_file_share(h: Harness) -> None:
    """多个 `file` 输入只附了一件材料时：共享同一附件，而不是把其余必填项判成缺失。"""
    name = f"{_TAG}-multifile-{uuid.uuid4().hex[:6]}"
    async with _probe_worker(name, inputs=_MULTI_FILE_INPUTS):
        conv = await h.new_conversation()
        filename = "报价单合集-2026Q1.xlsx"
        file_id = await h.seed_file(filename)
        run_id = await h.seed_run(
            conversation_id=conv,
            worker_name=name,
            attachment_ids=[file_id],
            timeout_seconds=60,
        )
        graph = _FakeGraph([lambda _g: _done("验收：材料已收到")])
        h.rt.graph = graph
        await h.rt._process_run(run_id)
        row = await h.fetch_run(run_id)
        contract = str(
            ((graph.last_config or {}).get("configurable") or {}).get("input_contract") or ""
        )
        h.report.add(
            "P1-4-multi-file-share",
            "一件附件顶多个 file 输入：两个必填项都拿到同一附件，且契约标注共用",
            expected="run 未被输入门拦下（status=done、图被调用 1 次）、契约里两个输入都取值到同一"
            "附件名、含「共用同一附件」标注",
            actual=f"status={row['status']}、图调用={graph.calls}、"
            f"error={(row['error'] or {}).get('code')}、"
            f"契约内附件名出现次数={contract.count(filename)}、"
            f"共用标注={'共用同一附件' in contract}",
            evidence=[line.strip() for line in contract.splitlines() if filename in line],
            passed=(
                row["status"] == "done"
                and graph.calls == 1
                and contract.count(filename) == 2
                and "共用同一附件" in contract
            ),
        )


# ---------- 任务级产物归属与视图（P0-5 收尾）----------

_ARTIFACT_TEXT = f"{_TAG} 探针：请汇总本次比价结论，并输出完整交付清单。"
_ARTIFACT_STEP_1 = "交付清单"
_ARTIFACT_STEP_2 = "复核报告"


def _long_text(marker: str) -> str:
    """超过外置阈值的正文：阈值从设置读，不写死数字——调阈值不会让验收假失败。"""
    from app.core.config import settings

    return f"{marker}：\n" + "明细结论一行；" * (settings.artifact_inline_max_chars // 3 + 1)


async def _scoped_run(
    h: Harness, conv: str, *, seq: int, step_name: str, marker: str
) -> tuple[str, str]:
    """同一任务下再造一个「子任务 + run」并在其中产出长正文（触发外置）。

    子任务归属取的是"第一个未收口的主线步骤"（与门同一口径），故调用方需先把上一步
    收口，否则第二个 run 会被算到第一个子任务头上。
    """
    run = await h.seed_run(conversation_id=conv, timeout_seconds=60, text=_ARTIFACT_TEXT)
    task_id = await h.task_id(conv)
    step_id = await h.seed_step(
        task_id,
        seq=seq,
        name=step_name,
        kind="main",
        status="doing",
        run_id=run,
    )
    h.rt.graph = _FakeGraph([lambda _g: _done(_long_text(marker))])
    await h.rt._process_run(run)
    return run, step_id


async def p0_5_task_artifacts(h: Harness) -> None:
    """P0-5 收尾：产物归属落库 + 任务级产物视图（跨本任务的 run 归集）。

    两件事分开断言：① 写入方必须把 `task_id`/`step_id` 写进去（否则任务级视图只能靠
    run 反查，且没法按子任务分组）；② 视图必须同时认「产物自带归属」和「子任务绑定的 run」
    两条腿——只看前者会让本次归属列之前的存量行集体消失，只看后者丢掉任务内新增挂载。
    """
    from app.modules.runs.models import RunArtifact

    conv = await h.new_conversation()
    run1, step1 = await _scoped_run(h, conv, seq=1, step_name=_ARTIFACT_STEP_1, marker="第一份成果")
    await h.step_status(step1, "done")
    run2, step2 = await _scoped_run(h, conv, seq=2, step_name=_ARTIFACT_STEP_2, marker="第二份成果")
    task_id = await h.task_id(conv)
    envelope = (await h.fetch_run(run1))["result"] or {}

    # 存量行：归属列（M10 之前）为空，只能靠 run 反查——真实老数据的形状
    legacy = str(uuid.uuid4())
    async with h.sf() as db:
        db.add(
            RunArtifact(
                id=uuid.UUID(legacy),
                run_id=uuid.UUID(run2),
                task_id=None,
                step_id=None,
                kind="text",
                name="存量产物（归属列之前）",
                mime="text/plain",
                size=12,
                storage="inline",
                payload={"text": "存量正文"},
                idempotency_key=f"{run2}:legacy-scope-probe",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await db.commit()

    async with h.sf() as db:
        rows = (
            await db.execute(
                h._text(
                    "SELECT id, task_id, step_id, idempotency_key FROM run_artifacts "
                    "WHERE run_id = :rid ORDER BY created_at DESC"
                ),
                {"rid": run1},
            )
        ).mappings()
        scoped = [dict(r) for r in rows.all()]
    first = scoped[0] if scoped else {}
    h.report.add(
        "P0-5-artifact-scope-write",
        "外置产物的写入方填 task_id/step_id：任务级视图不必靠反查 run 才能归属",
        expected=f"run1 至少 1 行产物，且每行的 task_id={task_id[:8]}…、step_id={step1[:8]}…"
        "（= 真子任务 id）、幂等键非空、结果信封带产物引用",
        actual=f"产物行数={len(scoped)}、task_id 对得上={first.get('task_id') == uuid.UUID(task_id)}、"
        f"step_id 对得上={first.get('step_id') == uuid.UUID(step1)}、"
        f"幂等键非空={bool(first.get('idempotency_key'))}、"
        f"信封引用数={len(envelope.get('artifacts') or [])}",
        evidence=[f"产物「{first.get('id')}」归属 {first.get('task_id')}/{first.get('step_id')}"]
        if first
        else ["run1 未落任何产物"],
        passed=(
            bool(scoped)
            and all(r["task_id"] == uuid.UUID(task_id) for r in scoped)
            and all(r["step_id"] == uuid.UUID(step1) for r in scoped)
            and all(r["idempotency_key"] for r in scoped)
            and len(envelope.get("artifacts") or []) == 1
        ),
    )

    resp = await h.client.get(f"/api/v1/tasks/{task_id}/artifacts")
    body = resp.json() if resp.status_code == 200 else []
    ids = [row.get("id") for row in body]
    # 存量行没有归属列，故只能按 run 反查；它同时是"前端会把老产物显示成未归属子任务"的样本
    legacy_row = next((row for row in body if row.get("id") == legacy), {})
    scoped_names = {row.get("step_name") for row in body if row.get("id") != legacy}
    created = [datetime.fromisoformat(row["created_at"]) for row in body]
    limited = await h.client.get(f"/api/v1/tasks/{task_id}/artifacts", params={"limit": 1})
    limited_body = limited.json() if limited.status_code == 200 else []
    missing = await h.client.get(f"/api/v1/tasks/{uuid.uuid4()}/artifacts")
    h.report.add(
        "P0-5-task-artifacts-view",
        "任务级产物视图：新行按归属、存量行按子任务绑的 run，都归集得到；倒序 + limit + 未知任务 404",
        expected="200、含 run1/run2 的产物 + 存量行（task_id 为空）共 3 条、新行的 step_name 分别补齐为"
        "两个真子任务名、存量行的 task_id 由路由兜底为任务 id 而 step_name 为空（前端显示未归属"
        "子任务）、created_at 倒序、limit=1 只回 1 条（最新）、未知 task 404",
        actual=f"HTTP={resp.status_code}、条数={len(ids)}、新行 step_name={sorted(scoped_names)}、"
        f"存量行 step_name={legacy_row.get('step_name')}、"
        f"存量行 task_id 兜底={legacy_row.get('task_id') == task_id}、"
        f"倒序={created == sorted(created, reverse=True)}、"
        f"存量行在内={legacy in ids}、limit=1 条数={len(limited_body)}、"
        f"未知 task HTTP={missing.status_code}",
        evidence=[
            f"{row.get('name')} ← 「{row.get('step_name') or '未归属子任务'}」" for row in body
        ],
        passed=(
            resp.status_code == 200
            and len(ids) == 3
            and len(set(ids)) == 3
            and legacy in ids
            and scoped_names == {_ARTIFACT_STEP_1, _ARTIFACT_STEP_2}
            and legacy_row.get("step_name") is None
            and legacy_row.get("task_id") == task_id
            and created == sorted(created, reverse=True)
            and len(limited_body) == 1
            and limited_body[0].get("id") == ids[0]
            and missing.status_code == 404
        ),
    )

    run_view = await h.client.get(f"/api/v1/runs/{run1}/artifacts")
    run_body = run_view.json() if run_view.status_code == 200 else []
    run_ids = [row.get("id") for row in run_body]
    task_side = [row for row in body if row.get("run_id") == run1]
    same_fields = [
        (row.get("name"), row.get("mime"), row.get("size"))
        == (tag.get("name"), tag.get("mime"), tag.get("size"))
        for row, tag in zip(run_body, task_side, strict=False)
    ]
    h.report.add(
        "P0-5-artifacts-run-consistency",
        "两个口径不打架：run 级清单与任务级清单里属于同一 run 的产物完全一致",
        expected=f"`/runs/{run1[:8]}…/artifacts` 的 id 顺序 == 任务级里 run_id={run1[:8]}… 的子集"
        "（各 1 条），且两处 name/mime/size 逐字一致（前端两处复用同一渲染）",
        actual=f"run 级条数={len(run_ids)}、任务级同 run 条数={len(task_side)}、"
        f"字段一致={all(same_fields)}",
        evidence=[f"run 级={run_ids}"],
        passed=(
            run_view.status_code == 200
            and len(run_ids) == 1
            and run_ids == [row.get("id") for row in task_side]
            and all(same_fields)
        ),
    )


# ---------- 分组表 ----------

GROUPS: dict[str, list[Callable[[Harness], Awaitable[None]]]] = {
    "ledger": [v3_ledger, v1_segmented_timeout],
    "await": [v6_await_roundtrip, v7_await_idempotency, v8_await_timeout, v9_restart_safety],
    "api": [
        m7a_legacy_task_backfill,
        p1_9_send_idempotent,
        v11_result_replay,
        p1_4_input_gate,
        p2_6_observability_views,
    ],
    "progress": [p1_5_progress],
    "converge": [p1_6_converge],
    "failure": [p0_3_failure_drills],
    "mcp": [p2_1_mcp_real],
    "budget": [p0_2_tool_budget_warning],
    "limits": [p1_2_limits_resolution],
    "step-gate": [p1_4_step_gate, p1_4_multi_file_share],
    "artifacts": [p0_5_task_artifacts],
}


# ---------- 入口 ----------


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v1.6 真实验收（方案 §9.1）")
    parser.add_argument("-g", "--groups", nargs="*", default=None, help="只跑指定分组")
    parser.add_argument("--list", action="store_true", help="列出全部条目")
    parser.add_argument("--keep", action="store_true", help="保留探针数据（默认清理）")
    args = parser.parse_args(argv)

    if args.list:
        for group, checks in GROUPS.items():
            for check in checks:
                print(f"{group:8s} {check.__name__}")
        return 0

    _prepare_env()
    if Path.cwd().name != "backend":  # `python -m scripts.*` 要求 backend/ 为工作目录
        print("请在 backend/ 目录下运行：uv run python -m scripts.accept_v16", file=sys.stderr)
        return 2

    groups = args.groups or list(GROUPS)
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        print(f"未知分组：{unknown}（可选 {list(GROUPS)}）", file=sys.stderr)
        return 2
    checks = [c for g in groups for c in GROUPS[g]]
    if not checks:
        print("没有可跑的条目", file=sys.stderr)
        return 2

    harness = Harness(keep=args.keep)
    t0 = time.monotonic()
    try:
        await harness.setup()
        for check in checks:
            print(f"\n—— {check.__name__} ——")
            try:
                await check(harness)
            except Exception as exc:  # noqa: BLE001 —— 单条炸掉不能让整轮失去结论
                import traceback

                harness.report.add(
                    check.__name__,
                    f"{check.__name__} 执行异常",
                    expected="正常产出 PASS/FAIL",
                    actual=f"{type(exc).__name__}: {exc}",
                    passed=False,
                    evidence=traceback.format_exc().strip().splitlines()[-6:],
                )
    finally:
        await harness.teardown()

    print(f"\n{'=' * 78}\n用时 {time.monotonic() - t0:.1f}s")
    print(harness.report.render())
    if harness.report.failed:
        print("\n验收不通过：请修代码，不要改断言。")
        return 1
    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
