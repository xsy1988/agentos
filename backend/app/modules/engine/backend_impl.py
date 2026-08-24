"""InProcessBackend —— EngineBackend 协议的一期进程内实现（M2-2a 冻结协议）。"""

import json
import uuid
from typing import Any

from sqlalchemy import text

from app.core.db import session_factory


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
