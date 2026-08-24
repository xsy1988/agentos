"""retriever —— 任务→Top-K 能力检索（模块详细设计 §1.2.2）。

- 输入：用户任务文本（+会话近几轮，当前取最近一条 human 消息）
- 输出：Top-K 能力（K 默认 8，受 Agent 的 tool_budget 约束在 assembler 封顶）
- 检索池：enabled 且健康的能力 ∪ Agent pinned 集合（去重在 assembler 做）
- 健康过滤交给下游：mcp 能力展开工具时经连接池，无连接自然摘除
- 无可用 embedding provider 时退化为 pinned-only（语义发现不可用但不阻断）
"""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import session_factory

DEFAULT_K = 8


async def _embed_query(query: str) -> list[float] | None:
    """用 kind=embedding 的 provider 生成查询向量；无 provider 返回 None。"""
    from app.modules.models_module.models import ModelProvider
    from app.modules.models_module.provider import decrypt_secret, get_embeddings

    async with session_factory() as db:
        prov = await db.scalar(
            select(ModelProvider).where(
                ModelProvider.kind == "embedding",
                ModelProvider.status == "enabled",
            )
        )
        if prov is None:
            return None
        api_key = decrypt_secret(prov.api_key_encrypted) if prov.api_key_encrypted else None
        embeddings = get_embeddings(prov, api_key)
    return await embeddings.aembed_query(query)


def _cap_dict(cap: Any) -> dict[str, Any]:
    """ORM → 引擎侧 dict（capability 粒度，payload 含 schema/transport 等）。"""
    return {
        "id": str(cap.id),
        "name": cap.name,
        "type": cap.type,
        "description": cap.description,
        "risk_level": cap.risk_level,
        "payload": cap.payload or {},
    }


async def retrieve_capabilities(
    query: str, agent_id: str, k: int = DEFAULT_K
) -> dict[str, list[dict[str, Any]]]:
    """语义 Top-K + Agent pinned 常驻。返回 {"semantic": [...], "pinned": [...]}。"""
    from app.modules.capabilities.models import Capability, CapabilityBinding

    vec = await _embed_query(query)
    pinned: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    async with session_factory() as db:
        pinned_rows = await db.scalars(
            select(Capability)
            .join(CapabilityBinding, CapabilityBinding.capability_id == Capability.id)
            .where(
                CapabilityBinding.agent_id == uuid.UUID(agent_id),
                CapabilityBinding.mode == "pinned",
                Capability.enabled.is_(True),  # noqa: E712
            )
        )
        pinned = [_cap_dict(c) for c in pinned_rows]
        if vec is not None:
            rows = await db.scalars(
                select(Capability)
                .where(
                    Capability.type.in_(("tool", "mcp", "plugin")),  # type: ignore[attr-defined]
                    Capability.enabled.is_(True),  # noqa: E712
                    Capability.embedding.isnot(None),
                )
                .order_by(Capability.embedding.cosine_distance(vec))
                .limit(k)
            )
            semantic = [_cap_dict(c) for c in rows]
    return {"semantic": semantic, "pinned": pinned}
