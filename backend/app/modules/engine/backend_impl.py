"""InProcessBackend —— EngineBackend 协议的一期进程内实现（M2-2a 冻结协议）。

增量扩展：M7a 任务架构、M9a 结果产物（P0-5，`save_artifact`/`list_artifacts`）、
M9b 外部等待（P0-4，`ensure_await` 等 4 个方法）。
"""

import json
import uuid
from typing import Any

from sqlalchemy import text

from app.core.db import session_factory
from app.modules.engine.models import Plan as PlanModel
from app.modules.tasks.models import Task as TaskModel


class InProcessBackend:
    """直接操作 DB 会话；二期拆进程时替换为 RPC 实现，图与钩子零改动。"""

    async def get_agent_config(self, agent_id: str) -> dict[str, Any]:
        from app.modules.agents.models import Agent
        from app.modules.models_module.models import ModelProvider

        async with session_factory() as db:
            agent = await db.get(Agent, uuid.UUID(agent_id))
            if agent is None:
                return {}
            provider: ModelProvider | None = None
            if agent.model_provider_id:
                provider = await db.get(ModelProvider, agent.model_provider_id)
            return {
                "id": str(agent.id),
                "name": agent.name,
                "soul_md": agent.soul_md,
                "identity_md": agent.identity_md,
                "memory_md": agent.memory_md,
                "system_prompt": agent.system_prompt,
                "tool_budget": agent.tool_budget,
                "max_iterations": agent.max_iterations,
                "max_tokens_per_run": agent.max_tokens_per_run,
                "timeout_seconds": agent.timeout_seconds,
                "provider": (
                    {
                        "id": str(provider.id),
                        "impl": provider.impl,
                        "base_url": provider.base_url,
                        "model_name": provider.model_name,
                        "params": provider.params,
                        # limits 随 provider 一起下发（P1-2）：engine 构造模型时登记进限流器
                        "limits": dict(provider.limits or {}),
                        "api_key_encrypted": (
                            bytes(provider.api_key_encrypted)
                            if provider.api_key_encrypted
                            else None
                        ),
                    }
                    if provider and provider.status == "enabled"
                    else None
                ),
            }

    async def get_model_provider(self, provider_id: str) -> dict[str, Any] | None:
        """run 级模型覆盖用：按 id 取单个可用 provider（结构同 agent_config.provider）。"""
        from app.modules.models_module.models import ModelProvider

        async with session_factory() as db:
            provider = await db.get(ModelProvider, uuid.UUID(provider_id))
            if provider is None or provider.status != "enabled":
                return None
            return {
                "id": str(provider.id),
                "impl": provider.impl,
                "base_url": provider.base_url,
                "model_name": provider.model_name,
                "params": provider.params,
                # limits 随 provider 一起下发（P1-2）：engine 构造模型时登记进限流器
                "limits": dict(provider.limits or {}),
                "api_key_encrypted": (
                    bytes(provider.api_key_encrypted) if provider.api_key_encrypted else None
                ),
            }

    async def get_lightweight_provider(self) -> dict[str, Any] | None:
        """轻量模型（params.lightweight 标记的 enabled llm，取第一个）：
        意图分类/规划/验收/闲聊回复等内部短调用用它降本，未配置返回 None。"""
        from sqlalchemy import select

        from app.modules.models_module.models import ModelProvider

        async with session_factory() as db:
            provider = await db.scalar(
                select(ModelProvider)
                .where(
                    ModelProvider.kind == "llm",
                    ModelProvider.status == "enabled",
                    ModelProvider.params["lightweight"].as_boolean() == True,  # noqa: E712
                )
                .order_by(ModelProvider.created_at)
                .limit(1)
            )
            if provider is None:
                return None
            return {
                "id": str(provider.id),
                "impl": provider.impl,
                "base_url": provider.base_url,
                "model_name": provider.model_name,
                "params": provider.params,
                # limits 随 provider 一起下发（P1-2）：engine 构造模型时登记进限流器
                "limits": dict(provider.limits or {}),
                "api_key_encrypted": (
                    bytes(provider.api_key_encrypted) if provider.api_key_encrypted else None
                ),
            }

    async def emit_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
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

    async def save_plan(self, run_id: str, items: list[dict[str, Any]]) -> str:
        from sqlalchemy import select

        from app.modules.engine.models import Plan

        run_uuid = uuid.UUID(run_id)
        async with session_factory() as db:
            plan = await db.scalar(select(Plan).where(Plan.run_id == run_uuid))
            if plan is None:
                plan = Plan(run_id=run_uuid, items=items)
                db.add(plan)
            else:
                plan.items = items
            await db.commit()
            await db.refresh(plan)
            return str(plan.id)

    async def load_plan(self, run_id: str) -> list[dict[str, Any]] | None:
        from sqlalchemy import select

        from app.modules.engine.models import Plan

        async with session_factory() as db:
            plan = await db.scalar(select(Plan).where(Plan.run_id == uuid.UUID(run_id)))
            return list(plan.items) if plan else None

    async def get_capability(self, name: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from app.modules.capabilities.models import Capability

        async with session_factory() as db:
            cap = await db.scalar(
                select(Capability).where(Capability.name == name, Capability.enabled.is_(True))
            )
            if cap is None:
                return None
            return {
                "id": str(cap.id),
                "type": cap.type,
                "name": cap.name,
                "payload": cap.payload,
                "risk_level": cap.risk_level,
            }

    async def list_enabled_tools(self) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.modules.capabilities.models import Capability

        async with session_factory() as db:
            rows = await db.scalars(
                select(Capability).where(
                    Capability.type == "tool",
                    Capability.enabled.is_(True),
                )
            )
            return [
                {
                    "id": str(c.id),
                    "name": c.name,
                    "description": c.description,
                    "payload": c.payload,
                    "risk_level": c.risk_level,
                }
                for c in rows
            ]

    # ---- M7a 增量扩展（ADR-23~28：任务架构）----

    async def ensure_task_id(self, conversation_id: str) -> str | None:
        from app.modules.tasks import service as tasks_service

        try:
            conv_id = uuid.UUID(conversation_id)
        except ValueError:
            return None
        async with session_factory() as db:
            task = await tasks_service.ensure_task_for_conversation(db, conv_id)
            if task is None:
                return None
            await db.commit()
            return str(task.id)

    async def get_task_context(self, task_id: str) -> dict[str, Any] | None:
        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            return await tasks_service.task_context(db, uuid.UUID(task_id))

    async def start_task_plan(self, task_id: str, run_id: str) -> list[dict[str, Any]] | None:
        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            task = await db.get(TaskModel, uuid.UUID(task_id))
            if task is None:
                return None
            items = await tasks_service.plan_items_from_steps(db, task, uuid.UUID(run_id))
            await db.commit()
            return items

    async def raise_subtask(
        self,
        task_id: str,
        run_id: str,
        *,
        name: str,
        description: str = "",
        question: str | None = None,
    ) -> dict[str, Any] | None:
        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            task = await db.get(TaskModel, uuid.UUID(task_id))
            if task is None:
                return None
            step = await tasks_service.raise_branch_step(
                db,
                task,
                name=name,
                description=description,
                question=question,
                run_id=uuid.UUID(run_id),
            )
            await db.commit()
            return {"step_id": str(step.id), "seq": step.seq, "name": step.name}

    async def answer_subtask(self, task_id: str, step_id: str, answer: str, run_id: str) -> bool:
        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            task = await db.get(TaskModel, uuid.UUID(task_id))
            if task is None:
                return False
            step = await tasks_service.answer_branch_step(
                db, task, uuid.UUID(step_id), answer, run_id=uuid.UUID(run_id)
            )
            await db.commit()
            return step is not None

    async def resolve_subtask(
        self,
        task_id: str,
        step_id: str,
        *,
        action: str,
        data: Any = None,
        applied: list | None = None,
        run_id: str,
    ) -> bool:
        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            task = await db.get(TaskModel, uuid.UUID(task_id))
            if task is None:
                return False
            step = await tasks_service.resolve_branch_decision(
                db,
                task,
                uuid.UUID(step_id),
                action=action,
                data=data,
                applied=applied,
                run_id=uuid.UUID(run_id),
            )
            await db.commit()
            return step is not None

    async def finalize_task_plan(self, task_id: str, run_id: str, *, achieved: bool) -> int:
        from sqlalchemy import select

        from app.modules.tasks import service as tasks_service

        async with session_factory() as db:
            task = await db.get(TaskModel, uuid.UUID(task_id))
            if task is None or not achieved:
                return 0
            plan = await db.scalar(select(PlanModel).where(PlanModel.run_id == uuid.UUID(run_id)))
            bound: list[uuid.UUID] = []
            for it in list(plan.items) if plan else []:
                raw = it.get("step_id")
                if not raw:
                    continue
                try:
                    bound.append(uuid.UUID(str(raw)))
                except ValueError:
                    continue
            closed = await tasks_service.advance_run_steps(
                db, task, uuid.UUID(run_id), step_ids=bound or None
            )
            await db.commit()
            return len(closed)

    # ---- M9a 增量扩展（方案 §4 P0-5：结果产物一等化）----

    async def save_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        name: str | None,
        mime: str | None,
        size: int,
        storage: str,
        payload: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy import select

        from app.modules.runs.models import RunArtifact

        async with session_factory() as db:
            if idempotency_key:
                existing = await db.scalar(
                    select(RunArtifact).where(RunArtifact.idempotency_key == idempotency_key)
                )
                if existing is not None:
                    return _artifact_brief(existing)
            artifact = RunArtifact(
                run_id=uuid.UUID(run_id),
                kind=kind,
                name=name,
                mime=mime,
                size=size,
                storage=storage,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            db.add(artifact)
            await db.commit()
            await db.refresh(artifact)
            return _artifact_brief(artifact)

    async def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.modules.runs.models import RunArtifact

        async with session_factory() as db:
            rows = await db.scalars(
                select(RunArtifact)
                .where(RunArtifact.run_id == uuid.UUID(run_id))
                .order_by(RunArtifact.created_at)
            )
            return [_artifact_brief(a) for a in rows]

    # ---- M9b 增量扩展（方案 §4 P0-4：外部等待一等化）----

    async def ensure_await(
        self,
        *,
        run_id: str,
        tool_name: str,
        idempotency_key: str,
        builtin: str,
        capability_id: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from app.modules.awaits import service as awaits_service
        from app.modules.runs.models import Run

        async with session_factory() as db:
            run = await db.get(Run, uuid.UUID(run_id))
            row, _created = await awaits_service.ensure_await(
                db,
                run_id=run_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                conversation_id=run.conversation_id if run else None,
                task_id=(run.input or {}).get("task_id") if run else None,
                capability_id=capability_id,
                payload_in={"builtin": builtin, "args": args or {}},
            )
            return _await_view(row, with_token=True)

    async def get_await(self, await_id: str) -> dict[str, Any] | None:
        from app.modules.awaits.models import AwaitBroker

        async with session_factory() as db:
            row = await db.get(AwaitBroker, uuid.UUID(await_id))
            return _await_view(row, with_token=True) if row is not None else None

    async def mark_await_dispatched(self, await_id: str, *, response: Any = None) -> None:
        from app.modules.awaits import service as awaits_service
        from app.modules.awaits.models import AwaitBroker

        async with session_factory() as db:
            row = await db.get(AwaitBroker, uuid.UUID(await_id))
            if row is None:
                return
            await awaits_service.mark_notified(
                db, row, response=response if isinstance(response, dict) else None
            )

    async def cancel_await(self, await_id: str, *, reason: str | None = None) -> bool:
        from app.modules.awaits import service as awaits_service
        from app.modules.awaits.models import AwaitBroker

        async with session_factory() as db:
            row = await db.get(AwaitBroker, uuid.UUID(await_id))
            if row is None:
                return False
            won = await awaits_service.cancel(db, row, reason=reason)
            await db.commit()
            return won


def _await_view(row: Any, *, with_token: bool = False) -> dict[str, Any]:
    """等待行摘要（引擎内部视图）：时间戳转 ISO，凭据仅在登记/重读时下发。"""
    payload_in = row.payload_in or {}
    view: dict[str, Any] = {
        "await_id": str(row.id),
        "run_id": str(row.run_id),
        "tool_name": row.tool_name,
        "builtin": payload_in.get("builtin"),
        "args": payload_in.get("args") or {},
        "dispatch_response": payload_in.get("dispatch_response"),
        "idempotency_key": row.idempotency_key,
        "status": row.status,
        "deadline_at": row.deadline_at.isoformat() if row.deadline_at else None,
        "notified_at": row.notified_at.isoformat() if row.notified_at else None,
        "payload_out": row.payload_out,
        "error": row.error,
        "waited_ms": int(row.waited_ms or 0),
    }
    if with_token:
        view["callback_token"] = row.callback_token
    return view


def _artifact_brief(artifact: Any) -> dict[str, Any]:
    """产物摘要（不含 payload 正文）：拼引用行与清单共用。"""
    return {
        "id": str(artifact.id),
        "run_id": str(artifact.run_id),
        "kind": artifact.kind,
        "name": artifact.name,
        "mime": artifact.mime,
        "size": int(artifact.size or 0),
        "storage": artifact.storage,
    }
