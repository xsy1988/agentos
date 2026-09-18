"""tasks 路由：主任务/子任务实例（任务看板的唯一数据源）。

Worker 定义层已文件化（/workers，app/modules/workers/router.py）；
本 router（/tasks）只管实例：创建（绑定 Worker 生效版本）、进度、看板分组。
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.conversations import service as conv_service
from app.modules.conversations.models import Conversation
from app.modules.runs import events as run_events
from app.modules.runs.models import NON_TERMINAL_RUN_STATUSES, Run, RunArtifact
from app.modules.runs.schemas import ArtifactOut, TaskArtifactOut
from app.modules.tasks import service
from app.modules.tasks.models import Task, TaskStep
from app.modules.tasks.schemas import (
    ConversationBrief,
    StepConvergeIn,
    StepCreateIn,
    StepUpdateIn,
    TaskCreateIn,
    TaskCreateOut,
    TaskDetailOut,
    TaskGroupOut,
    TaskOut,
    TaskStepOut,
    TaskUpdateIn,
    WorkerGroupBrief,
)
from app.modules.workers import registry
from app.modules.workers.inputs import InputContractError, sanitize_provided_inputs
from app.modules.workers.registry import COMMON_WORKER, COMMON_WORKER_DISPLAY

router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(get_current_user)],
)

# 未终态 run（看板据此显示「执行中 / 待确认 / 等待外部」）真源见 runs/models


# ---------- 序列化辅助 ----------


def _percent(done: int, total: int, task_status: str) -> int:
    """进度百分比：无步骤时按状态给 0/100，有步骤时四舍五入（确定性，非 LLM 估算）。"""
    if total <= 0:
        return 100 if task_status == "done" else 0
    return min(100, round(done * 100 / total))


async def _run_flags(db: AsyncSession, conv_ids: list[UUID]) -> dict[str, tuple[bool, UUID | None]]:
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
    """任务实例 → 出参（补齐 Worker 展示信息/会话摘要/待确认标记）。

    Worker 信息来自文件注册中心（缓存命中零开销）；未注册 Worker 优雅降级。
    """
    if not tasks:
        return []
    metas = {m.name: m for m in registry.list_workers()}
    conv_ids = [t.conversation_id for t in tasks if t.conversation_id]
    convs: dict[str, Conversation] = {}
    if conv_ids:
        rows = await db.scalars(select(Conversation).where(Conversation.id.in_(conv_ids)))
        convs = {str(c.id): c for c in rows.all()}
    flags = await _run_flags(db, conv_ids)
    awaiting_steps = await _awaiting_step_counts(db, [t.id for t in tasks])
    out: list[TaskOut] = []
    for t in tasks:
        meta = metas.get(t.worker_name)
        wdef = meta.def_ if meta is not None else None
        display = (
            COMMON_WORKER_DISPLAY
            if t.worker_name in ("", COMMON_WORKER)
            else (wdef.name if wdef is not None else t.worker_name)
        )
        conv = convs.get(str(t.conversation_id)) if t.conversation_id else None
        awaiting, active_run_id = flags.get(str(t.conversation_id), (False, None))
        pending_steps = awaiting_steps.get(str(t.id), 0)
        out.append(
            TaskOut(
                id=t.id,
                worker_name=t.worker_name,
                worker_display_name=display,
                worker_version=t.worker_version,
                worker_icon=wdef.icon if wdef is not None else None,
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


def _board_query():
    return (
        select(Task)
        .outerjoin(Conversation, Conversation.id == Task.conversation_id)
        .order_by(func.coalesce(Conversation.last_message_at, Task.created_at).desc())
    )


# ---------- 任务实例与看板（L2 实例层） ----------


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    worker_name: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[TaskOut]:
    """主任务实例列表（按最近活动排序）。"""
    stmt = _board_query()
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if worker_name is not None:
        stmt = stmt.where(Task.worker_name == worker_name)
    tasks = list((await db.scalars(stmt.limit(limit))).all())
    return await _decorate_tasks(db, tasks)


@router.get("/board", response_model=list[TaskGroupOut])
async def task_board(
    include_empty: bool = False,
    limit_per_group: int = Query(default=20, ge=1, le=100),
    include_closed_tasks: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[TaskGroupOut]:
    """任务看板：按 Worker 分组，组内为该 Worker 的任务实例（左侧对话记录的新形态）。

    已完成的实例默认保留（用户要回看结论），靠 limit_per_group 截断避免无限增长。
    分组头来自文件注册中心；指向已删除 Worker 的实例也保留分组（降级展示）。
    """
    tasks = list((await db.scalars(_board_query())).all())
    if not include_closed_tasks:
        tasks = [t for t in tasks if t.status == "active"]
    decorated = await _decorate_tasks(db, tasks)
    by_worker: dict[str, list[TaskOut]] = {}
    for item in decorated:
        by_worker.setdefault(item.worker_name, []).append(item)

    metas = {m.name: m for m in registry.list_workers()}
    groups: list[TaskGroupOut] = []

    def _brief(name: str) -> WorkerGroupBrief:
        meta = metas.get(name)
        if name in ("", COMMON_WORKER):
            return WorkerGroupBrief(
                name=COMMON_WORKER, display_name=COMMON_WORKER_DISPLAY, enabled=True
            )
        if meta is None:
            return WorkerGroupBrief(name=name, display_name=name, enabled=False)
        return WorkerGroupBrief(
            name=name,
            display_name=name,
            description=meta.def_.description if meta.def_ else "",
            icon=meta.def_.icon if meta.def_ else None,
            enabled=meta.enabled,
            active_version=meta.effective_version,
        )

    ordered_names: list[str] = [m.name for m in metas.values()]
    # 内建通用任务组：零选择会话的兑底分组，排在最后
    ordered_names.append(COMMON_WORKER)
    for name in ordered_names:
        items = by_worker.get(name, [])
        if name == COMMON_WORKER:
            items = by_worker.get(COMMON_WORKER, []) + by_worker.get("", [])
        if not items and not include_empty:
            continue
        groups.append(
            TaskGroupOut(
                worker=_brief(name), **_group_summary(items), tasks=items[:limit_per_group]
            )
        )
    # 兜底：实例指向已删除/未注册 Worker 时也要能看到（否则任务凭空消失）
    for name, items in by_worker.items():
        # 空串 = 旧数据未选 Worker，已并入通用任务组
        if name in ordered_names or name == "":
            continue
        groups.append(
            TaskGroupOut(
                worker=_brief(name), **_group_summary(items), tasks=items[:limit_per_group]
            )
        )
    return groups


@router.post("", response_model=TaskCreateOut, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreateIn, db: AsyncSession = Depends(get_db)) -> TaskCreateOut:
    """新建主任务：一把创建 会话 + 任务实例（含子任务骨架）(+ 首条 run)。

    客户端「新开会话并发送」/「新建任务」都走这里——一个主任务一个会话（ADR-26）。
    任务创建时锁定该 Worker 当前生效版本。
    """
    meta = registry.get_meta(body.worker_name)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Worker「{body.worker_name}」不存在")
    if not meta.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Worker「{body.worker_name}」已停用")
    agent = await conv_service.resolve_agent(db, body.agent_id)
    try:
        provided_inputs = sanitize_provided_inputs(body.inputs)
    except InputContractError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    model_override = await conv_service.resolve_model_override(db, body.model_provider_id)

    # 幂等（P1-9）：判重必须发生在建会话/任务之前——重复提交若走到 create_user_run
    # 才发现，会话与主任务骨架已经落库，前台的「连点两次」会多出空任务。
    key = conv_service.normalize_client_message_id(body.client_message_id)
    if key is not None:
        existing = await conv_service.find_run_by_client_message_id(db, key)
        if existing is not None:
            task_id = (existing.input or {}).get("task_id")
            task = await db.get(Task, UUID(str(task_id))) if task_id else None
            if task is None or existing.conversation_id is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "重复提交：该消息已处理")
            return TaskCreateOut(
                task=(await _decorate_tasks(db, [task]))[0],
                conversation_id=existing.conversation_id,
                run_id=existing.id,
            )

    # 未显式命名 → 保持默认标题，首条消息到达时自动命名并同步任务标题
    conv = Conversation(agent_id=agent.id, title=body.title or "新会话")
    db.add(conv)
    await db.flush()
    task = await service.create_task(
        db,
        worker_name=body.worker_name,
        agent_id=agent.id,
        title=body.title or body.worker_name,
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
            client_message_id=key,
            provided_inputs=provided_inputs,
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


@router.get("/{task_id}/artifacts", response_model=list[TaskArtifactOut])
async def list_task_artifacts(
    task_id: UUID,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[TaskArtifactOut]:
    """任务级产物视图（P0-5 收尾）：跨本任务的 run 归集产物，正文按需取 `/artifacts/{id}`。

    归集口径两条腿并用：产物自身的 `task_id`（新写入路径）+ 子任务绑定的 run（`task_steps.run_id`，
    覆盖本次归属列之前的存量行）。只看 `task_id` 会让老数据集体消失，只看 run 则丢掉"任务内
    新增/人工挂载"的归属。排序按时间倒序（最新成果在最上），`limit` 挡住超大任务的一次性拉取。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    steps = list((await db.scalars(select(TaskStep).where(TaskStep.task_id == task_id))).all())
    run_ids = [s.run_id for s in steps if s.run_id is not None]
    conditions = [RunArtifact.task_id == task_id]
    if run_ids:
        conditions.append(RunArtifact.run_id.in_(run_ids))
    stmt = (
        select(RunArtifact)
        .where(or_(*conditions))
        .order_by(RunArtifact.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.scalars(stmt)).all())
    step_names = {s.id: s.name for s in steps}
    return [
        TaskArtifactOut(
            **ArtifactOut.model_validate(a).model_dump(),
            task_id=a.task_id or task_id,
            step_id=a.step_id,
            step_name=step_names.get(a.step_id) if a.step_id else None,
        )
        for a in rows
    ]


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
        task = await service.update_step_status(db, step, body.status, resolution=body.resolution)
    elif body.resolution is not None:
        step.resolution = body.resolution
    await db.commit()
    await db.refresh(task)
    return await _task_detail(db, task)


@router.post("/{task_id}/steps/{step_id}/converge", response_model=TaskDetailOut)
async def converge_task_step(
    task_id: UUID,
    step_id: UUID,
    body: StepConvergeIn,
    db: AsyncSession = Depends(get_db),
) -> TaskDetailOut:
    """前台收敛受阻支线（P1-6）：关闭支线 / 重新排队 / 转人工。

    替代「人工 SQL 改 task_steps」：状态与 `resolution.reason`（恒为 manual 枚举）
    一次落库，并在该支线所属 run 的事件流里留痕（close/requeue → unblocked，
    escalate → blocked），SSE 与事件重放因此都能看到收敛动作。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    try:
        step = await service.converge_blocked_step(
            db, task, step_id, action=body.action, detail=body.detail
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if step is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "子任务不存在")
    # 事件载荷必须在 commit 前取（commit 会让 ORM 实例过期）
    event_payload = {
        "step_id": str(step.id),
        "task_id": str(task.id),
        "name": step.name,
        "action": body.action,
        "reason": "manual",
        "status": step.status,
    }
    # 收敛动作是「关于该 run 的事实」：附着在支线所属 run 的事件流上。
    # 无 run 归属的支线（人工添加/模板遗留）没有可附着的事件流，只落状态（§5 P1-6）。
    if step.run_id is not None:
        if body.action == "escalate":
            await run_events.emit_event(step.run_id, "blocked", event_payload, db=db)
        else:
            await run_events.emit_event(step.run_id, "unblocked", event_payload, db=db)
    await db.commit()
    await db.refresh(task)
    return await _task_detail(db, task)
