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


async def embed_text(query: str) -> list[float] | None:
    """对外公开的文本向量化入口（复用 embedding provider）。

    供任务架构「新主任务检测」（tasks.service.detect_task_switch）等模块做语义比对；
    无可用 embedding provider 时返回 None，调用方据此静默降级（不阻断主流程）。
    """
    return await _embed_query(query)


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
    """主任务归属上下文：域内能力 id、通用兑底能力 id、当前主线步骤的建议能力名。

    Worker 定义已文件化（workers.registry）：
    - domain_ids = 任务绑定 Worker 的 WORKER.md capabilities 名单按名解析的 capability id；
    - common_ids = 未被任何启用 Worker 引用的能力（替代原「通用任务集」归属表）；
    - hints = 当前主线步骤（worker_step_ref）对应 sub_worker 的 capability_hint。
    """
    from app.modules.capabilities.models import Capability
    from app.modules.tasks.models import Task, TaskStep
    from app.modules.tasks.service import get_worker_def
    from app.modules.workers import registry

    try:
        task = await db.get(Task, uuid.UUID(str(task_id)))
    except ValueError:
        return None
    if task is None:
        return None

    # 全局引用集合（启用 Worker 生效版本）：一次算出，做通用/全局分档
    referenced_names = registry.referenced_capability_names()
    domain_names: set[str] = set()
    wdef = get_worker_def(task.worker_name, task.worker_version)
    if wdef is not None:
        domain_names = set(wdef.capabilities)

    rows = await db.execute(
        select(Capability.id, Capability.name).where(
            Capability.name.in_(referenced_names | domain_names)
        )
    )
    id_by_name = {name: str(cid) for cid, name in rows.all()}
    domain_ids: set[str] = {id_by_name[n] for n in domain_names if n in id_by_name}
    # 通用兑底档：未被任何启用 Worker 引用的能力（原「通用任务集」归属的等价语义）
    common_rows = await db.scalars(
        select(Capability.id).where(~Capability.name.in_(referenced_names | domain_names))
    )
    common_ids: set[str] = {str(cid) for cid in common_rows}

    # 当前主线步骤的建议能力名：从 sub_worker 的 capability_hint 读
    hints: list[str] = []
    if wdef is not None:
        steps = list(
            (
                await db.scalars(
                    select(TaskStep)
                    .where(
                        TaskStep.task_id == task.id,
                        TaskStep.kind == "main",
                        TaskStep.status.in_(("pending", "doing")),
                    )
                    .order_by(TaskStep.seq)
                    .limit(1)
                )
            ).all()
        )
        if steps and steps[0].worker_step_ref:
            sub = wdef.find_sub(steps[0].worker_step_ref)  # type: ignore[arg-type]
            if sub is not None:
                for name in sub.capability_hint:
                    if isinstance(name, str) and name not in hints:
                        hints.append(name)
    return {
        "worker_name": task.worker_name,
        "worker_version": task.worker_version,
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
