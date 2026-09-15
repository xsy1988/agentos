"""tasks 业务逻辑：模板 seed、任务实例、子任务状态机、进度重算、任务卡渲染。

设计要点（设计方案 ADR-23~28）：
- 进度由子任务状态**确定性**重算，绝不由 LLM 估算；
- 主任务实例与会话 1:1，旧会话/迁移遗漏由 `ensure_task_for_conversation` 惰性补建；
- 支线子任务复用「先判定 → 写库」纪律，状态迁移全部收敛到本模块，避免多处散写。
"""

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.modules.capabilities.models import Capability
from app.modules.tasks.models import (
    COMMON_TASK_TYPE_NAME,
    Task,
    TaskStep,
    TaskType,
    TaskTypeCapability,
    TaskTypeStep,
)

# 进度分子的状态：done 与 skipped（跳过的步骤不应把完成度永久压在 100% 以下）
PROGRESS_COUNTED_STATUSES = ("done", "skipped")

COMMON_TASK_TYPE_DESCRIPTION = (
    "通用任务集：不属于任何特定主任务的能力与历史会话都归于此，"
    "作为所有主任务的能力兜底。"
)


# ---------- 模板（L1） ----------


async def seed_common_task_type() -> uuid.UUID:
    """幂等 seed 内建「通用任务集」，返回其 id（应用启动时调用）。"""
    async with session_factory() as db:
        existing = await db.scalar(
            select(TaskType).where(TaskType.name == COMMON_TASK_TYPE_NAME)
        )
        if existing is None:
            existing = TaskType(
                name=COMMON_TASK_TYPE_NAME,
                description=COMMON_TASK_TYPE_DESCRIPTION,
                kind="common",
                sort_order=0,
                enabled=True,
            )
            db.add(existing)
        await db.commit()
        common_id = existing.id
    # 兜底：任何未归属主任务的能力补挂「通用任务」（保证软约束第三档总有回退）
    await attach_unowned_capabilities_common(common_id)
    return common_id


async def attach_unowned_capabilities_common(common_id: uuid.UUID) -> int:
    """把尚无任何归属的能力挂到「通用任务」，返回新挂数量。"""
    async with session_factory() as db:
        rows = await db.execute(
            select(Capability.id)
            .outerjoin(
                TaskTypeCapability, TaskTypeCapability.capability_id == Capability.id
            )
            .where(TaskTypeCapability.id.is_(None))
        )
        missing = [r[0] for r in rows.all()]
        for cap_id in missing:
            db.add(TaskTypeCapability(task_type_id=common_id, capability_id=cap_id))
        if missing:
            await db.commit()
    return len(missing)


async def get_common_task_type_id(db: AsyncSession) -> uuid.UUID:
    """取「通用任务集」id（seed 保证存在；缺失则现场补建）。"""
    tid = await db.scalar(select(TaskType.id).where(TaskType.name == COMMON_TASK_TYPE_NAME))
    if tid is None:
        return await seed_common_task_type()
    return tid


def _template_order(tpl: TaskTypeStep) -> tuple[int, int]:
    """主线在前、支线在后，各自按 seq。"""
    return (0 if tpl.kind == "main" else 1, tpl.seq)


async def load_step_templates(db: AsyncSession, task_type_id: uuid.UUID) -> list[TaskTypeStep]:
    rows = list(
        (
            await db.scalars(
                select(TaskTypeStep).where(TaskTypeStep.task_type_id == task_type_id)
            )
        ).all()
    )
    return sorted(rows, key=_template_order)


# ---------- 实例（L2） ----------


async def instantiate_steps(
    db: AsyncSession, task: Task, templates: Sequence[TaskTypeStep]
) -> list[TaskStep]:
    """按模板生成子任务骨架：主线 + 支线模板全部实例化（支线初始 pending 待触发）。

    支线一起实例化的理由：看板要能一眼看出「这个主任务预计会涉及哪些支线」，
    且 raise_subtask 可复用同名的模板步骤，避免重复建步骤。
    """
    created: list[TaskStep] = []
    for i, tpl in enumerate(sorted(templates, key=_template_order), start=1):
        step = TaskStep(
            task_id=task.id,
            seq=i,
            name=tpl.name,
            description=tpl.description or "",
            kind=tpl.kind,
            status="pending",
            source="template",
            template_step_id=tpl.id,
        )
        db.add(step)
        created.append(step)
    await db.flush()
    task.progress_total = len(created)
    task.progress_done = 0
    return created


async def create_task(
    db: AsyncSession,
    *,
    task_type_id: uuid.UUID,
    agent_id: uuid.UUID,
    title: str,
    conversation_id: uuid.UUID | None = None,
) -> Task:
    """新建主任务实例并实例化步骤骨架（调用方负责 commit）。"""
    tpl = await db.get(TaskType, task_type_id)
    if tpl is None:
        raise ValueError("主任务模板不存在")
    task = Task(
        task_type_id=task_type_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        title=title or "新任务",
        status="active",
        progress_done=0,
        progress_total=0,
        out_of_scope=[],
        started_at=datetime.now(UTC),
    )
    db.add(task)
    await db.flush()
    await instantiate_steps(db, task, await load_step_templates(db, task_type_id))
    return task


async def get_task_by_conversation(
    db: AsyncSession, conversation_id: uuid.UUID
) -> Task | None:
    return await db.scalar(select(Task).where(Task.conversation_id == conversation_id))


async def ensure_task_for_conversation(
    db: AsyncSession, conversation: Any
) -> Task:
    """会话 → 主任务实例；旧会话（迁移遗漏/并发新建）惰性补建为「通用任务」。"""
    task = await get_task_by_conversation(db, conversation.id)
    if task is not None:
        return task
    common_id = await get_common_task_type_id(db)
    return await create_task(
        db,
        task_type_id=common_id,
        agent_id=conversation.agent_id,
        title=conversation.title,
        conversation_id=conversation.id,
    )


async def sync_task_title(db: AsyncSession, conversation_id: uuid.UUID, title: str) -> None:
    """会话自动命名时同步任务标题。

    只在会话仍是默认标题（「新会话」）触发自动命名时调用，所以这里直接覆盖：
    用户显式命名过的任务/会话不会走到这条路径。
    """
    task = await get_task_by_conversation(db, conversation_id)
    if task is not None and title:
        task.title = title


async def append_out_of_scope(db: AsyncSession, task: Task, text: str) -> None:
    """记录一次「用户坚持在本会话执行其他主任务」的越界请求。"""
    entries = list(task.out_of_scope or [])
    entries.append({"text": text[:500], "at": datetime.now(UTC).isoformat()})
    # 只留最近 20 条，避免无界增长
    task.out_of_scope = entries[-20:]


# ---------- 子任务状态机与进度 ----------


async def recompute_progress(
    db: AsyncSession, task: Task, *, autoclose: bool = True
) -> list[TaskStep]:
    """按步骤状态重算进度；主线全部收口时自动跳过未触发的支线并置任务完成。"""
    steps = list(
        (await db.scalars(select(TaskStep).where(TaskStep.task_id == task.id))).all()
    )
    main_steps = [s for s in steps if s.kind == "main"]
    # 仍有子任务进行中/待用户答复/受阻时不算收口：澄清型支线是阻塞闸门，引擎正暂停等
    # 答复，此时若把主任务判完成，看板会与 run 状态自相矛盾（ADR-24/ADR-28）。
    has_open_step = any(s.status in ("doing", "awaiting_user", "blocked") for s in steps)
    if (
        autoclose
        and main_steps
        and not has_open_step
        and all(s.status in PROGRESS_COUNTED_STATUSES for s in main_steps)
    ):
        # 主线全部收口 → 未触发的支线自动跳过，进度才能收敛到 100%
        for s in steps:
            if s.kind == "branch" and s.status == "pending":
                s.status = "skipped"
        if task.status == "active":
            task.status = "done"
            task.finished_at = datetime.now(UTC)
    task.progress_total = len(steps)
    task.progress_done = sum(1 for s in steps if s.status in PROGRESS_COUNTED_STATUSES)
    return steps


async def update_step_status(
    db: AsyncSession,
    step: TaskStep,
    status: str,
    *,
    resolution: dict | None = None,
    run_id: uuid.UUID | None = None,
    autoclose: bool = True,
) -> Task:
    """子任务状态迁移的唯一入口（写状态 → 补时间戳 → 重算任务进度）。"""
    prev = step.status
    step.status = status
    if resolution is not None:
        step.resolution = resolution
    if run_id is not None:
        step.run_id = run_id
    now = datetime.now(UTC)
    if status == "awaiting_user" and prev != "awaiting_user":
        step.raised_at = now
    if status in ("done", "skipped", "blocked"):
        step.resolved_at = now
    task = await db.get(Task, step.task_id)
    if task is None:  # 理论上不可能（FK 保证）；防 def 分支崩溃
        raise ValueError("子任务所属主任务不存在")
    await recompute_progress(db, task, autoclose=autoclose)
    return task


_NORM_RE = re.compile(r"[\s　]+")


def _norm_name(name: str) -> str:
    return _NORM_RE.sub("", name or "").strip().lower()


async def find_or_create_branch_step(
    db: AsyncSession,
    task: Task,
    *,
    name: str,
    description: str = "",
    run_id: uuid.UUID | None = None,
) -> TaskStep:
    """支线子任务去重：同名未收口的支线复用，避免 interrupt 重放重复建步骤。

    重放安全（ADR-24）：interrupt 恢复会整节点重放，故同一 run 内出现过同名支线
    一律复用（不论其当前状态），否则重放会再建一条同名支线、把任务清单撑爆。
    """
    steps = list(
        (await db.scalars(select(TaskStep).where(TaskStep.task_id == task.id))).all()
    )
    target = _norm_name(name)
    for s in steps:
        if s.kind != "branch" or _norm_name(s.name) != target:
            continue
        if run_id is not None and s.run_id == run_id:
            if description and not s.description:
                s.description = description
            return s
        if s.status in ("pending", "doing", "awaiting_user"):
            if description and not s.description:
                s.description = description
            if run_id is not None:
                s.run_id = run_id
            return s
    seq = max((s.seq for s in steps), default=0) + 1
    step = TaskStep(
        task_id=task.id,
        seq=seq,
        name=name,
        description=description,
        kind="branch",
        status="pending",
        source="agent_raised",
        run_id=run_id,
        raised_at=datetime.now(UTC),
    )
    db.add(step)
    await db.flush()
    await recompute_progress(db, task, autoclose=False)
    return step


async def apply_run_plan(
    db: AsyncSession,
    task: Task,
    run_id: uuid.UUID,
    items: Sequence[dict[str, Any]],
) -> list[TaskStep]:
    """run 终态回写：把 plans.items 里已绑定 step_id 的完成项推进到 done。

    items 形如 [{seq, text, status, step_id?}]——step_id 是加法扩展键，
    未绑定的计划项（纯 planner 拆解）不影响任务步骤。
    """
    if not items:
        return []
    steps = {
        str(s.id): s
        for s in (await db.scalars(select(TaskStep).where(TaskStep.task_id == task.id))).all()
    }
    touched: list[TaskStep] = []
    for it in items:
        step_id = it.get("step_id")
        if not step_id:
            continue
        step = steps.get(str(step_id))
        if step is None:
            continue
        status = str(it.get("status") or "")
        if status == "done" and step.status not in PROGRESS_COUNTED_STATUSES:
            step.status = "done"
            step.resolved_at = datetime.now(UTC)
            step.run_id = step.run_id or run_id
            touched.append(step)
        elif status in ("doing", "pending") and step.status == "pending":
            step.status = "doing"
            step.run_id = step.run_id or run_id
            touched.append(step)
    if touched:
        await recompute_progress(db, task)
    return touched


# ---------- 任务卡（注入 system_prompt 的永不压缩区） ----------


# ---------- 引擎接口（P4：run 生命周期 ↔ 子任务状态机） ----------


async def _task_steps(db: AsyncSession, task_id: uuid.UUID) -> list[TaskStep]:
    return list(
        (
            await db.scalars(
                select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.seq)
            )
        ).all()
    )


async def plan_items_from_steps(
    db: AsyncSession, task: Task, run_id: uuid.UUID | None = None, *, limit: int = 5
) -> list[dict[str, Any]] | None:
    """把主任务的**未收口主线步骤**转成计划项（带 step_id 绑定），并启动第一步。

    主线任务模板即计划来源：复杂任务不再让 planner 凭空拆解，而是沿用户/管理员
    预先定义的架构推进（ADR-23）。返回 None 表示本主任务已无待办主线，
    调用方回退到自由规划。front 端看板与 plans 表看到的是同一份计划。
    """
    steps = await _task_steps(db, task.id)
    open_main = [s for s in steps if s.kind == "main" and s.status not in PROGRESS_COUNTED_STATUSES]
    if not open_main:
        return None
    picked = open_main[:limit]
    items: list[dict[str, Any]] = []
    for i, s in enumerate(picked, start=1):
        status = "doing" if i == 1 else "pending"
        items.append(
            {
                "seq": i,
                "text": s.name,
                "status": status,
                "step_id": str(s.id),
            }
        )
    first = picked[0]
    if first.status == "pending":
        await update_step_status(db, first, "doing", run_id=run_id, autoclose=False)
    elif run_id is not None and first.run_id is None:
        first.run_id = run_id
    return items


async def advance_run_steps(
    db: AsyncSession,
    task: Task,
    run_id: uuid.UUID,
    *,
    step_ids: Sequence[uuid.UUID] | None = None,
    advance_first_open: bool = True,
) -> list[TaskStep]:
    """run 成功收尾时推进主线：绑定步骤置 done；无绑定时推进第一个未收口主线步骤。"""
    steps = await _task_steps(db, task.id)
    closed: list[TaskStep] = []
    targets: list[TaskStep] = []
    if step_ids:
        wanted = {str(s) for s in step_ids}
        targets = [s for s in steps if str(s.id) in wanted]
    if not targets and advance_first_open:
        for s in steps:
            if s.kind == "main" and s.status not in PROGRESS_COUNTED_STATUSES:
                targets = [s]
                break
    for s in targets:
        if s.status in PROGRESS_COUNTED_STATUSES:
            continue
        await update_step_status(db, s, "done", run_id=s.run_id or run_id)
        closed.append(s)
    return closed


async def resolve_task_step(
    db: AsyncSession,
    task: Task,
    step_id: uuid.UUID,
    *,
    status: str = "done",
    resolution: dict | None = None,
    run_id: uuid.UUID | None = None,
) -> TaskStep | None:
    """按 id 收敛子任务状态（引擎侧 ask_user 答复回填的唯一入口）。"""
    step = await db.get(TaskStep, step_id)
    if step is None or step.task_id != task.id:
        return None
    await update_step_status(db, step, status, resolution=resolution, run_id=run_id)
    return step


async def raise_branch_step(
    db: AsyncSession,
    task: Task,
    *,
    name: str,
    description: str = "",
    question: str | None = None,
    run_id: uuid.UUID | None = None,
) -> TaskStep:
    """引擎侧抛出支线子任务（ADR-24）：blocking 澄清型直接置 awaiting_user。

    question 存进 resolution.question：看板与任务卡都能显示「在等用户答复什么」。
    """
    step = await find_or_create_branch_step(
        db, task, name=name, description=description, run_id=run_id
    )
    if question:
        payload = dict(step.resolution or {})
        payload["question"] = question
        step.resolution = payload
    if step.status != "awaiting_user":
        await update_step_status(db, step, "awaiting_user", run_id=run_id, autoclose=False)
    return step


async def answer_branch_step(
    db: AsyncSession,
    task: Task,
    step_id: uuid.UUID,
    answer: str,
    *,
    run_id: uuid.UUID | None = None,
) -> TaskStep | None:
    """用户答复回填：支线置 done 并把答复固化到 resolution（供任务卡与看板展示）。"""
    step = await db.get(TaskStep, step_id)
    if step is None or step.task_id != task.id:
        return None
    payload = dict(step.resolution or {})
    payload["answer"] = answer
    payload["at"] = datetime.now(UTC).isoformat()
    return await resolve_task_step(
        db, task, step.id, status="done", resolution=payload, run_id=run_id
    )


async def task_context(db: AsyncSession, task_id: uuid.UUID) -> dict[str, Any] | None:
    """引擎侧任务上下文：任务卡文本 + 步骤清单（装配进 protected 区）。"""
    task = await db.get(Task, task_id)
    if task is None:
        return None
    tpl = await db.get(TaskType, task.task_type_id)
    steps = await _task_steps(db, task.id)
    return {
        "task_id": str(task.id),
        "task_type_id": str(task.task_type_id),
        "task_type_name": tpl.name if tpl else task.title,
        "title": task.title,
        "status": task.status,
        "progress_done": task.progress_done,
        "progress_total": task.progress_total,
        "card": await render_task_card(db, task_id),
        "steps": [
            {
                "id": str(s.id),
                "seq": s.seq,
                "name": s.name,
                "kind": s.kind,
                "status": s.status,
                "question": (s.resolution or {}).get("question"),
            }
            for s in steps
        ],
    }


async def render_task_card(db: AsyncSession, task_id: uuid.UUID) -> str:
    """渲染「当前主任务」区文本：目标 + 进度 + 子任务清单（含用户已答复内容）。"""
    task = await db.get(Task, task_id)
    if task is None:
        return ""
    tpl = await db.get(TaskType, task.task_type_id)
    steps = list(
        (
            await db.scalars(
                select(TaskStep)
                .where(TaskStep.task_id == task.id)
                .order_by(TaskStep.seq)
            )
        ).all()
    )
    branch_no = 0
    branch_index: dict[str, int] = {}
    lines: list[str] = []
    for s in steps:
        if s.kind == "branch":
            branch_no += 1
            branch_index[str(s.id)] = branch_no
            label = f"B{branch_no}"
            suffix = "（支线）"
        else:
            label = str(s.seq)
            suffix = ""
        answer = ""
        if s.resolution and s.resolution.get("answer"):
            answer = f"（用户答复：{str(s.resolution['answer'])[:200]}）"
        lines.append(f"[{s.status}] {label} {s.name}{suffix}{answer}")
    header = f"主任务：{tpl.name if tpl else task.title}"
    if tpl is not None and tpl.description:
        header += f" — {tpl.description}"
    body = "\n".join(lines) if lines else "（本主任务尚未定义子任务模板）"
    out_of_scope = ""
    if task.out_of_scope:
        out_of_scope = (
            "\n注意：用户曾在本会话中要求处理本主任务之外的请求，"
            "请专注于当前主任务；如确认是新的主任务，应建议用户新开会话。"
        )
    return (
        f"# 当前主任务\n{header}\n"
        f"进度：{task.progress_done}/{task.progress_total}\n"
        f"子任务：\n{body}{out_of_scope}"
    )


async def counts_for_task_types(db: AsyncSession) -> dict[str, dict[str, int]]:
    """模板列表用的聚合计数：模板 id → {capability_count, task_count}。"""
    out: dict[str, dict[str, int]] = {}
    cap_rows = await db.execute(
        select(TaskTypeCapability.task_type_id, func.count())
        .group_by(TaskTypeCapability.task_type_id)
    )
    for tid, cnt in cap_rows.all():
        out.setdefault(str(tid), {})["capability_count"] = int(cnt)
    task_rows = await db.execute(
        select(Task.task_type_id, func.count()).group_by(Task.task_type_id)
    )
    for tid, cnt in task_rows.all():
        out.setdefault(str(tid), {})["task_count"] = int(cnt)
    return out
