"""tasks 路由：主任务模板（定义层） + 主任务/子任务实例（任务看板的唯一数据源）。

两个 router：
- `task_types_router`（/task-types）：人工维护的主任务模板与步骤模板、能力归属；
- `router`（/tasks）：主任务实例、子任务步骤、进度、看板分组。
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.capabilities.models import Capability
from app.modules.conversations import service as conv_service
from app.modules.conversations.models import Conversation
from app.modules.runs.models import Run
from app.modules.tasks import service
from app.modules.tasks.models import (
    COMMON_TASK_TYPE_NAME,
    Task,
    TaskStep,
    TaskType,
    TaskTypeCapability,
    TaskTypeStep,
)
from app.modules.tasks.schemas import (
    CapabilityBindIn,
    ConversationBrief,
    StepCreateIn,
    StepTemplateOut,
    StepTemplateReplaceIn,
    StepUpdateIn,
    TaskCreateIn,
    TaskCreateOut,
    TaskDetailOut,
    TaskGroupOut,
    TaskOut,
    TaskStepOut,
    TaskTypeCapabilityOut,
    TaskTypeCreateIn,
    TaskTypeOut,
    TaskTypeUpdateIn,
    TaskUpdateIn,
)

task_types_router = APIRouter(
    prefix="/task-types",
    tags=["task-types"],
    dependencies=[Depends(get_current_user)],
)
router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(get_current_user)],
)

# 未终态 run：看板据此显示「执行中 / 待确认」
NON_TERMINAL_RUN_STATUSES = ("pending", "running", "paused_awaiting_confirm")


# ---------- 序列化辅助 ----------


def _percent(done: int, total: int, task_status: str) -> int:
    """进度百分比：无步骤时按状态给 0/100，有步骤时四舍五入（确定性，非 LLM 估算）。"""
    if total <= 0:
        return 100 if task_status == "done" else 0
    return min(100, round(done * 100 / total))


async def _run_flags(
    db: AsyncSession, conv_ids: list[UUID]
) -> dict[str, tuple[bool, UUID | None]]:
    """会话 → (是否存在待确认 run, 最新活跃 run_id)。

    未终态 run 数量极小（单进程引擎），一次查完即可，无需 per-会话查询。
    """
    if not conv_ids:
        return {}
    rows = await db.execute(
        select(Run.conversation_id, Run.id, Run.status)
        .where(Run.conversation_id.in_(conv_ids), Run.status.in_(NON_TERMINAL_RUN_STATUSES))
        .order_by(Run.created_at.desc())
    )
    out: dict[str, tuple[bool, UUID | None]] = {}
    for conv_id, run_id, run_status in rows.all():
        awaiting, active = out.get(str(conv_id), (False, None))
        awaiting = awaiting or run_status == "paused_awaiting_confirm"
        out[str(conv_id)] = (awaiting, active if active is not None else run_id)
    return out


async def _awaiting_step_counts(db: AsyncSession, task_ids: list[UUID]) -> dict[str, int]:
    """任务 → awaiting_user 子任务数（run 可能已结束，但支线仍等用户答复）。"""
    if not task_ids:
        return {}
    rows = await db.execute(
        select(TaskStep.task_id, func.count())
        .where(TaskStep.task_id.in_(task_ids), TaskStep.status == "awaiting_user")
        .group_by(TaskStep.task_id)
    )
    return {str(task_id): int(cnt) for task_id, cnt in rows.all()}


async def _decorate_tasks(db: AsyncSession, tasks: list[Task]) -> list[TaskOut]:
    """任务实例 → 出参（补齐模板信息/会话摘要/待确认标记）。"""
    if not tasks:
        return []
    types = {str(t.id): t for t in (await db.scalars(select(TaskType))).all()}
    conv_ids = [t.conversation_id for t in tasks if t.conversation_id]
    convs: dict[str, Conversation] = {}
    if conv_ids:
        rows = await db.scalars(select(Conversation).where(Conversation.id.in_(conv_ids)))
        convs = {str(c.id): c for c in rows.all()}
    flags = await _run_flags(db, conv_ids)
    awaiting_steps = await _awaiting_step_counts(db, [t.id for t in tasks])
    out: list[TaskOut] = []
    for t in tasks:
        tpl = types.get(str(t.task_type_id))
        conv = convs.get(str(t.conversation_id)) if t.conversation_id else None
        awaiting, active_run_id = flags.get(str(t.conversation_id), (False, None))
        pending_steps = awaiting_steps.get(str(t.id), 0)
        out.append(
            TaskOut(
                id=t.id,
                task_type_id=t.task_type_id,
                task_type_name=tpl.name if tpl else "（模板已删除）",
                task_type_icon=tpl.icon if tpl else None,
                task_type_color=tpl.color if tpl else None,
                agent_id=t.agent_id,
                title=t.title,
                status=t.status,
                progress_done=t.progress_done,
                progress_total=t.progress_total,
                progress_percent=_percent(t.progress_done, t.progress_total, t.status),
                out_of_scope_count=len(t.out_of_scope or []),
                conversation=(
                    ConversationBrief(
                        id=conv.id,
                        title=conv.title,
                        status=conv.status,
                        message_count=conv.message_count,
                        last_message_at=conv.last_message_at,
                    )
                    if conv is not None
                    else None
                ),
                awaiting_confirm=awaiting or pending_steps > 0,
                awaiting_steps_count=pending_steps,
                active_run_id=active_run_id,
                started_at=t.started_at,
                finished_at=t.finished_at,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
        )
    return out


async def _task_detail(db: AsyncSession, task: Task) -> TaskDetailOut:
    base = (await _decorate_tasks(db, [task]))[0]
    steps = list(
        (
            await db.scalars(
                select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.seq)
            )
        ).all()
    )
    run_ids: list[UUID] = []
    if task.conversation_id:
        run_ids = list(
            (
                await db.scalars(
                    select(Run.id)
                    .where(Run.conversation_id == task.conversation_id)
                    .order_by(Run.created_at.desc())
                    .limit(20)
                )
            ).all()
        )
    return TaskDetailOut(
        **base.model_dump(),
        steps=[TaskStepOut.model_validate(s) for s in steps],
        run_ids=run_ids,
    )


async def _task_types_out(db: AsyncSession, types: list[TaskType]) -> list[TaskTypeOut]:
    if not types:
        return []
    ids = [t.id for t in types]
    steps = list(
        (
            await db.scalars(
                select(TaskTypeStep)
                .where(TaskTypeStep.task_type_id.in_(ids))
                .order_by(TaskTypeStep.seq)
            )
        ).all()
    )
    grouped: dict[str, list[StepTemplateOut]] = {}
    for s in steps:
        grouped.setdefault(str(s.task_type_id), []).append(StepTemplateOut.model_validate(s))
    counts = await service.counts_for_task_types(db)
    out: list[TaskTypeOut] = []
    for t in types:
        c = counts.get(str(t.id), {})
        item = TaskTypeOut.model_validate(t)
        item.steps = grouped.get(str(t.id), [])
        item.capability_count = c.get("capability_count", 0)
        item.task_count = c.get("task_count", 0)
        out.append(item)
    return out


async def _get_task_type(db: AsyncSession, task_type_id: UUID) -> TaskType:
    tpl = await db.get(TaskType, task_type_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "主任务模板不存在")
    return tpl


async def _replace_step_templates(
    db: AsyncSession, tpl: TaskType, items: list
) -> None:
    """整表替换步骤模板（模板量小，避免逐条 CRUD 的排序竞态）。

    已实例化的 task_steps.template_step_id 是 SET NULL，删模板不会带走历史实例。
    """
    await db.execute(delete(TaskTypeStep).where(TaskTypeStep.task_type_id == tpl.id))
    for i, item in enumerate(items, start=1):
        db.add(
            TaskTypeStep(
                task_type_id=tpl.id,
                seq=i,
                name=item.name.strip(),
                description=item.description,
                kind=item.kind,
                optional=item.optional,
                capability_hint=item.capability_hint,
            )
        )
    await db.flush()


# ---------- 主任务模板（L1 定义层） ----------


@task_types_router.get("", response_model=list[TaskTypeOut])
async def list_task_types(
    enabled_only: bool = False, db: AsyncSession = Depends(get_db)
) -> list[TaskTypeOut]:
    """主任务模板列表（含步骤模板 + 能力/实例计数），按 sort_order 排序。"""
    stmt = select(TaskType).order_by(TaskType.sort_order, TaskType.name)
    if enabled_only:
        stmt = stmt.where(TaskType.enabled.is_(True))
    return await _task_types_out(db, list((await db.scalars(stmt)).all()))


@task_types_router.post("", response_model=TaskTypeOut, status_code=status.HTTP_201_CREATED)
async def create_task_type(
    body: TaskTypeCreateIn, db: AsyncSession = Depends(get_db)
) -> TaskTypeOut:
    exists = await db.scalar(select(TaskType.id).where(TaskType.name == body.name))
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"主任务「{body.name}」已存在")
    tpl = TaskType(
        name=body.name,
        description=body.description,
        kind="business",
        icon=body.icon,
        color=body.color,
        sort_order=body.sort_order,
        default_agent_id=body.default_agent_id,
        enabled=body.enabled,
    )
    db.add(tpl)
    await db.flush()
    if body.steps:
        await _replace_step_templates(db, tpl, body.steps)
    # 新模板默认继承「通用任务」的能力集合：新主任务立刻可用，再按需增删
    common_id = await service.get_common_task_type_id(db)
    common_caps = await db.scalars(
        select(TaskTypeCapability.capability_id).where(
            TaskTypeCapability.task_type_id == common_id
        )
    )
    for cap_id in common_caps.all():
        db.add(TaskTypeCapability(task_type_id=tpl.id, capability_id=cap_id))
    await db.commit()
    await db.refresh(tpl)
    return (await _task_types_out(db, [tpl]))[0]


@task_types_router.get("/{task_type_id}", response_model=TaskTypeOut)
async def get_task_type(
    task_type_id: UUID, db: AsyncSession = Depends(get_db)
) -> TaskTypeOut:
    tpl = await _get_task_type(db, task_type_id)
    return (await _task_types_out(db, [tpl]))[0]


@task_types_router.patch("/{task_type_id}", response_model=TaskTypeOut)
async def update_task_type(
    task_type_id: UUID, body: TaskTypeUpdateIn, db: AsyncSession = Depends(get_db)
) -> TaskTypeOut:
    tpl = await _get_task_type(db, task_type_id)
    if tpl.kind == "common" and body.enabled is False:
        raise HTTPException(status.HTTP_409_CONFLICT, "通用任务集不可停用（能力兜底依赖它）")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != tpl.name:
        exists = await db.scalar(select(TaskType.id).where(TaskType.name == data["name"]))
        if exists is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"主任务「{data['name']}」已存在")
    for k, v in data.items():
        setattr(tpl, k, v)
    await db.commit()
    await db.refresh(tpl)
    return (await _task_types_out(db, [tpl]))[0]


@task_types_router.delete("/{task_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_type(task_type_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    """删除模板：通用任务集不可删；仍有任务实例的模板也不可删（先归档/删实例）。"""
    tpl = await _get_task_type(db, task_type_id)
    if tpl.kind == "common" or tpl.name == COMMON_TASK_TYPE_NAME:
        raise HTTPException(status.HTTP_409_CONFLICT, "通用任务集不可删除")
    used = await db.scalar(
        select(func.count()).select_from(Task).where(Task.task_type_id == tpl.id)
    )
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"该主任务下仍有 {used} 个任务实例，无法删除"
        )
    await db.delete(tpl)  # 步骤模板/能力归属随 FK CASCADE 清除
    await db.commit()


@task_types_router.put("/{task_type_id}/steps", response_model=TaskTypeOut)
async def replace_step_templates(
    task_type_id: UUID,
    body: StepTemplateReplaceIn,
    db: AsyncSession = Depends(get_db),
) -> TaskTypeOut:
    """整表替换子任务模板（主线 + 支线）。仅影响**之后**新建的任务实例。"""
    tpl = await _get_task_type(db, task_type_id)
    await _replace_step_templates(db, tpl, body.steps)
    await db.commit()
    return (await _task_types_out(db, [tpl]))[0]


@task_types_router.get("/{task_type_id}/capabilities", response_model=list[TaskTypeCapabilityOut])
async def list_task_type_capabilities(
    task_type_id: UUID, db: AsyncSession = Depends(get_db)
) -> list[TaskTypeCapabilityOut]:
    """该主任务归属的能力清单（域内检索偏置的依据）。"""
    await _get_task_type(db, task_type_id)
    rows = await db.execute(
        select(Capability)
        .join(TaskTypeCapability, TaskTypeCapability.capability_id == Capability.id)
        .where(TaskTypeCapability.task_type_id == task_type_id)
        .order_by(Capability.type, Capability.name)
    )
    return [
        TaskTypeCapabilityOut(
            capability_id=c.id,
            name=c.name,
            type=c.type,
            risk_level=c.risk_level,
            enabled=c.enabled,
        )
        for c in rows.scalars().all()
    ]


@task_types_router.post(
    "/{task_type_id}/capabilities",
    response_model=list[TaskTypeCapabilityOut],
    status_code=status.HTTP_201_CREATED,
)
async def bind_task_type_capabilities(
    task_type_id: UUID,
    body: CapabilityBindIn,
    db: AsyncSession = Depends(get_db),
) -> list[TaskTypeCapabilityOut]:
    """追加能力归属（幂等：已归属的跳过）。"""
    await _get_task_type(db, task_type_id)
    rows = await db.scalars(select(Capability.id).where(Capability.id.in_(body.capability_ids)))
    found = set(rows.all())
    missing = [str(i) for i in body.capability_ids if i not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"能力不存在：{', '.join(missing)}")
    existing = set(
        (
            await db.scalars(
                select(TaskTypeCapability.capability_id).where(
                    TaskTypeCapability.task_type_id == task_type_id
                )
            )
        ).all()
    )
    for cap_id in found - existing:
        db.add(TaskTypeCapability(task_type_id=task_type_id, capability_id=cap_id))
    await db.commit()
    return await list_task_type_capabilities(task_type_id, db)


@task_types_router.delete(
    "/{task_type_id}/capabilities/{capability_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unbind_task_type_capability(
    task_type_id: UUID, capability_id: UUID, db: AsyncSession = Depends(get_db)
) -> None:
    """解除归属；若该能力已无任何归属，自动挂回「通用任务」（保证兜底档非空）。"""
    await _get_task_type(db, task_type_id)
    await db.execute(
        delete(TaskTypeCapability).where(
            TaskTypeCapability.task_type_id == task_type_id,
            TaskTypeCapability.capability_id == capability_id,
        )
    )
    await db.commit()
    rows = await db.scalar(
        select(func.count())
        .select_from(TaskTypeCapability)
        .where(TaskTypeCapability.capability_id == capability_id)
    )
    if not rows:
        await service.attach_unowned_capabilities_common(
            await service.get_common_task_type_id(db)
        )


# ---------- 任务实例与看板（L2 实例层） ----------


def _board_query():
    return (
        select(Task)
        .outerjoin(Conversation, Conversation.id == Task.conversation_id)
        .order_by(func.coalesce(Conversation.last_message_at, Task.created_at).desc())
    )


def _group_summary(items: list[TaskOut]) -> dict[str, int]:
    """组级汇总：实例数 / 进行中 / 待确认 / 加权进度（按步数）。"""
    total_steps = sum(i.progress_total for i in items)
    done_steps = sum(i.progress_done for i in items)
    return {
        "task_count": len(items),
        "active_count": sum(1 for i in items if i.status == "active"),
        "awaiting_confirm_count": sum(1 for i in items if i.awaiting_confirm),
        "progress_percent": min(100, round(done_steps * 100 / total_steps)) if total_steps else 0,
    }


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    task_type_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[TaskOut]:
    """主任务实例列表（按最近活动排序）。"""
    stmt = _board_query()
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if task_type_id is not None:
        stmt = stmt.where(Task.task_type_id == task_type_id)
    tasks = list((await db.scalars(stmt.limit(limit))).all())
    return await _decorate_tasks(db, tasks)


@router.get("/board", response_model=list[TaskGroupOut])
async def task_board(
    include_empty: bool = False,
    limit_per_group: int = Query(default=20, ge=1, le=100),
    include_closed_tasks: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[TaskGroupOut]:
    """任务看板：按主任务分组，组内为该主任务的任务实例（左侧对话记录的新形态）。

    已完成的实例默认保留（用户要回看结论），靠 limit_per_group 截断避免无限增长。
    """
    types = list(
        (
            await db.scalars(
                select(TaskType).where(TaskType.enabled.is_(True)).order_by(
                    TaskType.sort_order, TaskType.name
                )
            )
        ).all()
    )
    tasks = list((await db.scalars(_board_query())).all())
    if not include_closed_tasks:
        tasks = [t for t in tasks if t.status == "active"]
    decorated = await _decorate_tasks(db, tasks)
    by_type: dict[str, list[TaskOut]] = {}
    for item in decorated:
        by_type.setdefault(str(item.task_type_id), []).append(item)
    types_out = {str(t.id): t for t in await _task_types_out(db, types)}
    groups: list[TaskGroupOut] = []
    for tpl in types:
        items = by_type.get(str(tpl.id), [])
        if not items and not include_empty:
            continue
        groups.append(
            TaskGroupOut(
                task_type=types_out[str(tpl.id)],
                **_group_summary(items),
                tasks=items[:limit_per_group],
            )
        )
    # 兜底：实例指向已停用模板时也要能看到（否则任务凭空消失）
    known = {str(t.id) for t in types}
    for tid, items in by_type.items():
        if tid in known:
            continue
        groups.append(
            TaskGroupOut(
                task_type=TaskTypeOut(
                    id=items[0].task_type_id,
                    name=items[0].task_type_name,
                    description="（模板已停用或删除）",
                    kind="business",
                    icon=items[0].task_type_icon,
                    color=items[0].task_type_color,
                    sort_order=9999,
                    default_agent_id=None,
                    enabled=False,
                    created_at=items[0].created_at,
                    updated_at=items[0].updated_at,
                ),
                **_group_summary(items),
                tasks=items[:limit_per_group],
            )
        )
    return groups


@router.post("", response_model=TaskCreateOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreateIn, db: AsyncSession = Depends(get_db)) -> TaskCreateOut:
    """新建主任务：一把创建 会话 + 任务实例（含子任务骨架）(+ 首条 run)。

    客户端「新开会话并发送」/「新建任务」都走这里——一个主任务一个会话（ADR-26）。
    """
    tpl = await _get_task_type(db, body.task_type_id)
    if not tpl.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"主任务「{tpl.name}」已停用")
    agent = await conv_service.resolve_agent(db, body.agent_id or tpl.default_agent_id)
    model_override = await conv_service.resolve_model_override(db, body.model_provider_id)

    # 未显式命名 → 保持默认标题，首条消息到达时自动命名并同步任务标题
    conv = Conversation(agent_id=agent.id, title=body.title or "新会话")
    db.add(conv)
    await db.flush()
    task = await service.create_task(
        db,
        task_type_id=tpl.id,
        agent_id=agent.id,
        title=body.title or tpl.name,
        conversation_id=conv.id,
    )
    run = None
    if body.text.strip():
        run = await conv_service.create_user_run(
            db,
            conv,
            agent,
            text=body.text,
            attachments=[],
            model_override=model_override,
            task=task,
        )
    await db.commit()
    await db.refresh(task)
    return TaskCreateOut(
        task=(await _decorate_tasks(db, [task]))[0],
        conversation_id=conv.id,
        run_id=run.id if run is not None else None,
    )


@router.get("/{task_id}", response_model=TaskDetailOut)
async def get_task(task_id: UUID, db: AsyncSession = Depends(get_db)) -> TaskDetailOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return await _task_detail(db, task)


@router.patch("/{task_id}", response_model=TaskDetailOut)
async def update_task(
    task_id: UUID, body: TaskUpdateIn, db: AsyncSession = Depends(get_db)
) -> TaskDetailOut:
    """重命名 / 归档任务（cancelled = 用户主动放弃）。"""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"]:
        task.title = data["title"]
    if "status" in data and data["status"]:
        task.status = data["status"]
        task.finished_at = None if data["status"] == "active" else datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
    return await _task_detail(db, task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: UUID, db: AsyncSession = Depends(get_db)) -> None:
    """删除任务实例（子任务随 CASCADE 清除）；会话与消息保留，不受影响。"""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    await db.delete(task)
    await db.commit()


@router.get("/{task_id}/steps", response_model=list[TaskStepOut])
async def list_task_steps(task_id: UUID, db: AsyncSession = Depends(get_db)) -> list[TaskStepOut]:
    rows = await db.scalars(
        select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.seq)
    )
    return [TaskStepOut.model_validate(s) for s in rows.all()]


@router.post("/{task_id}/steps", response_model=TaskDetailOut, status_code=status.HTTP_201_CREATED)
async def create_task_step(
    task_id: UUID, body: StepCreateIn, db: AsyncSession = Depends(get_db)
) -> TaskDetailOut:
    """人工添加子任务（source=user）。"""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    max_seq = await db.scalar(
        select(func.coalesce(func.max(TaskStep.seq), 0)).where(TaskStep.task_id == task.id)
    )
    db.add(
        TaskStep(
            task_id=task.id,
            seq=int(max_seq or 0) + 1,
            name=body.name.strip(),
            description=body.description,
            kind=body.kind,
            status="pending",
            source="user",
        )
    )
    await db.flush()
    await service.recompute_progress(db, task)
    await db.commit()
    await db.refresh(task)
    return await _task_detail(db, task)


@router.patch("/{task_id}/steps/{step_id}", response_model=TaskDetailOut)
async def update_task_step(
    task_id: UUID,
    step_id: UUID,
    body: StepUpdateIn,
    db: AsyncSession = Depends(get_db),
) -> TaskDetailOut:
    """改子任务状态（人工在任务卡上点完成/跳过，或补记用户答复）。"""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    step = await db.get(TaskStep, step_id)
    if step is None or step.task_id != task.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "子任务不存在")
    if body.status is not None:
        task = await service.update_step_status(
            db, step, body.status, resolution=body.resolution
        )
    elif body.resolution is not None:
        step.resolution = body.resolution
    await db.commit()
    await db.refresh(task)
    return await _task_detail(db, task)
