"""retriever —— 任务→Top-K 能力检索（模块详细设计 §1.2.2）。

- 输入：用户任务文本（+会话近几轮，当前取最近一条 human 消息）+ 主任务归属
- 输出：Top-K 能力（K 默认 8，受 Agent 的 tool_budget 约束在 assembler 封顶）
- 检索池：enabled 且健康的能力 ∪ Agent pinned 集合（去重在 assembler 做）
- 归属分层（ADR-28）：主任务域（模板步骤建议能力最优先）→ 通用任务集 → 全局；
  先放大候选再重排，避免域内能力被全局高分挤掉，也让无关域的能力排在最后
- 健康过滤交给下游：mcp 能力展开工具时经连接池，无连接自然摘除
- 无可用 embedding provider 时退化为 pinned-only（语义发现不可用但不阻断）
"""

import uuid
from typing import Any

from sqlalchemy import select

from app.core.db import session_factory

DEFAULT_K = 8
# 候选放大倍数：语义召回 k 条不足以在分层重排后仍有域内能力可用，故先取 k*3 条再分层截断
CANDIDATE_FACTOR = 3
# 能力归属分层（写入 capability_cache 的 source 字段，用于装配结果溯源）
SCOPES = ("task_domain", "task_common", "global")


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


def order_by_scope(
    cands: list[dict[str, Any]],
    *,
    domain_ids: set[str],
    common_ids: set[str],
    hints: tuple[str, ...] = (),
    k: int = DEFAULT_K,
) -> list[dict[str, Any]]:
    """按归属分层重排候选（纯函数，便于单测）。

    层内保持传入顺序（即向量距离序）：主任务域 → 通用任务集 → 全局。
    域内被模板步骤 `capability_hint` 点名的能力再提到最前（题名优先，不排除其它域内能力）。
    """
    domain = [c for c in cands if c["id"] in domain_ids]
    common = [c for c in cands if c["id"] not in domain_ids and c["id"] in common_ids]
    rest = [c for c in cands if c["id"] not in domain_ids and c["id"] not in common_ids]
    if hints:
        named = set(hints)
        domain.sort(key=lambda c: 0 if c["name"] in named else 1)
    out: list[dict[str, Any]] = []
    for group, scope in ((domain, SCOPES[0]), (common, SCOPES[1]), (rest, SCOPES[2])):
        for cap in group:
            cap["scope"] = scope
            out.append(cap)
    return out[:k]


async def _task_scope(db: Any, task_id: str) -> dict[str, Any] | None:
    """主任务归属上下文：域内能力 id、通用任务集能力 id、当前主线步骤的建议能力名。"""
    from app.modules.tasks.models import (
        COMMON_TASK_TYPE_NAME,
        Task,
        TaskStep,
        TaskType,
        TaskTypeCapability,
        TaskTypeStep,
    )

    try:
        task = await db.get(Task, uuid.UUID(str(task_id)))
    except ValueError:
        return None
    if task is None:
        return None
    common_id = await db.scalar(select(TaskType.id).where(TaskType.name == COMMON_TASK_TYPE_NAME))
    type_ids = [task.task_type_id] + ([common_id] if common_id else [])
    rows = await db.execute(
        select(TaskTypeCapability.task_type_id, TaskTypeCapability.capability_id).where(
            TaskTypeCapability.task_type_id.in_(type_ids)
        )
    )
    domain_ids: set[str] = set()
    common_ids: set[str] = set()
    for tid, cid in rows.all():
        (domain_ids if tid == task.task_type_id else common_ids).add(str(cid))
    raw_hints = await db.scalars(
        select(TaskTypeStep.capability_hint)
        .join(TaskStep, TaskStep.template_step_id == TaskTypeStep.id)
        .where(
            TaskStep.task_id == task.id,
            TaskStep.kind == "main",
            TaskStep.status.in_(("pending", "doing")),
        )
        .order_by(TaskStep.seq)
        .limit(1)
    )
    hints: list[str] = []
    for hint in raw_hints:
        for name in hint or []:
            if isinstance(name, str) and name not in hints:
                hints.append(name)
    return {
        "task_type_id": str(task.task_type_id),
        "domain_ids": domain_ids,
        "common_ids": common_ids,
        "hints": tuple(hints),
    }


async def retrieve_capabilities(
    query: str, agent_id: str, k: int = DEFAULT_K, task_id: str | None = None
) -> dict[str, Any]:
    """语义 Top-K + Agent pinned 常驻，语义结果按主任务归属分层。

    返回 {"semantic": [...], "pinned": [...], "scope": {...}|None}；
    条目带 `scope`（task_domain / task_common / global），assembler 据此写 source。
    """
    from app.modules.capabilities.models import Capability, CapabilityBinding

    vec = await _embed_query(query)
    pinned: list[dict[str, Any]] = []
    semantic: list[dict[str, Any]] = []
    scope: dict[str, Any] | None = None
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
                    Capability.type.in_(["tool", "mcp", "plugin", "skill"]),  # noqa: E712
                    Capability.enabled.is_(True),  # noqa: E712
                    Capability.embedding.isnot(None),
                )
                .order_by(Capability.embedding.cosine_distance(vec))
                .limit(k * CANDIDATE_FACTOR)
            )
            semantic = [_cap_dict(c) for c in rows]
            if task_id:
                scope = await _task_scope(db, task_id)
            if scope:
                semantic = order_by_scope(
                    semantic,
                    domain_ids=scope["domain_ids"],
                    common_ids=scope["common_ids"],
                    hints=scope["hints"],
                    k=k,
                )
            else:
                semantic = semantic[:k]
    return {"semantic": semantic, "pinned": pinned, "scope": scope}
