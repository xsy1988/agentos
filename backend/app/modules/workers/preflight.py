"""run 级输入预检（P1-4）：调用模型**之前**把「必需输入」判定完。

为什么是预检而不是提示词纪律：Worker 声明了必需输入却没拿到时，模型只会拿着
空上下文硬干（编数据 / 反问用户 / 反复试错），三种都比一次确定性拦截更贵。
所以判定必须在图之外完成，取值只来自平台能确定性判定的事实：

1. `run.input["inputs"]`（本次或本会话历史 run 的显式取值，API 写入时已校验）；
2. 本会话附件文件名（`file` 类型输入按声明顺序顶替——附件是确定性事实）；
3. 同会话历史 run 的快照（跨轮累积：用户第一轮给了报价单，第三轮不该被拦）。

缺失 → run 显式失败（`missing_inputs` 结构化错误 + 缺失字段清单），图与模型都不调用；
齐备 → 渲染契约文本进 `configurable.input_contract`，由 `context_assembly` 注入固定区，
模型看到的是「平台已预检 + 实测取值」，而不是猜自己要什么。

**子任务级门（`resolve_step_inputs`）**：子任务契约只有执行到那一步才明确，此刻 run 已
产出进度，硬失败等于把已做的工作扔掉；故 step 级缺输入走 `interrupt-ask`（图内
`input_gate` 节点），run 置 `paused_awaiting_confirm` 等用户补，补齐后从暂停点恢复。
取值来源与 run 级门完全同一套 `_collect`（会话内按输入名累积），不引入第二套口径。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.modules.files.models import File
from app.modules.runs.models import Run
from app.modules.tasks.models import TaskStep
from app.modules.workers import registry
from app.modules.workers.inputs import (
    InputSpec,
    missing_inputs_text,
    render_input_contract,
    resolve_inputs,
)

logger = logging.getLogger(__name__)

# 子任务契约在 system_prompt 里的起始标记：`input_gate` 节点在 interrupt 恢复时会被
# 重放，靠它把上一轮注入的契约整段替换掉，避免同一段文本随恢复次数叠加。
STEP_CONTRACT_HEADER = "## 当前子任务输入契约"


def strip_step_contract(system_prompt: str) -> str:
    """去掉上一轮注入的子任务契约段（无则原样返回）。"""
    index = system_prompt.find(STEP_CONTRACT_HEADER)
    return system_prompt[:index].rstrip() if index >= 0 else system_prompt


@dataclass(frozen=True)
class RunInputResolution:
    """一次预检的结论：声明、取值、缺失项与可直接注入的契约文本。"""

    worker_name: str
    worker_version: str | None
    specs: list[InputSpec]
    values: dict[str, str]
    missing: list[InputSpec]
    contract: str

    @property
    def ok(self) -> bool:
        """必需输入是否齐备（False 时禁止调用图）。"""
        return not self.missing

    @property
    def missing_names(self) -> list[str]:
        """缺失字段名（结构化错误载荷用，前端可据此渲染补输入入口）。"""
        return [s.name for s in self.missing]

    @property
    def message(self) -> str:
        """给用户看的缺失清单。"""
        return missing_inputs_text(self.missing)

    def inputs_payload(self) -> list[dict[str, Any]]:
        """全部声明的明细（含是否已提供），供错误载荷/排障展示。"""
        return [
            {
                **spec.to_dict(),
                "provided": spec.name in self.values,
                "value": self.values.get(spec.name),
            }
            for spec in self.specs
        ]


@dataclass(frozen=True)
class StepInputResolution:
    """一次子任务级预检的结论：哪一步、声明、取值、缺失项与可直接注入的契约文本。

    与 `RunInputResolution` 共用声明/取值/缺失三件套，差别只在**缺失时的处置**：
    run 级硬失败，step 级交给用户补（见模块 docstring）。
    """

    step_id: UUID
    step_name: str
    sub_ref: str
    specs: list[InputSpec]
    values: dict[str, str]
    missing: list[InputSpec]

    @property
    def ok(self) -> bool:
        """本子任务的必需输入是否齐备。"""
        return not self.missing

    @property
    def missing_names(self) -> list[str]:
        """缺失字段名（interrupt 载荷用）。"""
        return [s.name for s in self.missing]

    @property
    def message(self) -> str:
        """给用户看的补输入说明（不复用 run 级文案：此处模型可能已调用过，措辞不能骗人）。"""
        lines = [
            f"子任务「{self.step_name}」缺少必需输入，补齐后从暂停点继续（已产出的进度不重跑）："
        ]
        for spec in self.missing:
            row = f"- {spec.label}"
            if spec.description:
                row += f"：{spec.description}"
            if spec.type == "file":
                row += "（请随消息附带该文件）"
            lines.append(row)
        return "\n".join(lines)

    @property
    def contract(self) -> str:
        """注入固定区的文本（带子任务标题；无声明返回空串）。"""
        body = render_input_contract(self.specs, self.values)
        if not body:
            return ""
        return f"{STEP_CONTRACT_HEADER}（{self.step_name} / {self.sub_ref}）\n{body}\n"

    def payload(self) -> dict[str, Any]:
        """interrupt 载荷（前端据此渲染补输入表单；键序稳定，便于审计）。"""
        return {
            "step_id": str(self.step_id),
            "step_name": self.step_name,
            "sub_ref": self.sub_ref,
            "missing": self.missing_names,
            "message": self.message,
            "inputs": [
                {
                    **spec.to_dict(),
                    "label": spec.label,
                    "provided": spec.name in self.values,
                    "value": self.values.get(spec.name),
                }
                for spec in self.specs
            ],
        }


async def resolve_run_inputs(
    run: Run, *, db: AsyncSession | None = None
) -> RunInputResolution | None:
    """预检本 run 的输入契约；该 run 没有输入声明时返回 None（不做任何拦截）。

    `db` 仅测试注入用：生产路径自带只读会话，不参与调用方事务（预检是旁路，
    失败也不该连带回滚 run 的创建）。
    """
    payload = run.input or {}
    worker_name = payload.get("worker_name")
    if not worker_name:
        return None
    wdef = registry.get_def(str(worker_name), payload.get("worker_version"))
    if wdef is None or not wdef.inputs:
        return None

    if db is not None:
        provided, attachment_names = await _collect(run, db)
    else:
        async with session_factory() as own:
            provided, attachment_names = await _collect(run, own)

    values, missing = resolve_inputs(wdef.inputs, provided, attachment_names)
    return RunInputResolution(
        worker_name=wdef.name,
        worker_version=wdef.version,
        specs=list(wdef.inputs),
        values=values,
        missing=missing,
        contract=render_input_contract(wdef.inputs, values),
    )


async def resolve_step_inputs(
    run: Run, *, db: AsyncSession | None = None
) -> StepInputResolution | None:
    """预检**当前子任务**的输入契约；无子任务或该子任务无输入声明时返回 None。

    当前子任务 = 本主任务第一个未收口的主线步骤（`kind='main'` 且 doing/pending，
    取 seq 最小者，doing 优先）——与 `discovery.retriever` 的能力提示、
    `tasks.service` 的子任务指引取的是同一步，避免「门看的步骤」与「模型做的步骤」
    不是同一个。取值按输入名在**会话内**累积（与 run 级门同一命名空间）：
    用户第一轮给过的材料，后续子任务不必重复提供。

    `db` 仅测试注入用（同 `resolve_run_inputs`）。
    """
    payload = run.input if isinstance(run.input, Mapping) else {}
    task_id = payload.get("task_id")
    worker_name = payload.get("worker_name")
    if not task_id or not worker_name:
        return None
    try:
        tid = UUID(str(task_id))
    except (TypeError, ValueError):
        return None
    wdef = registry.get_def(str(worker_name), payload.get("worker_version"))
    if wdef is None:
        return None

    if db is not None:
        step, provided, attachment_names = await _collect_step(tid, run, db)
    else:
        async with session_factory() as own:
            step, provided, attachment_names = await _collect_step(tid, run, own)

    if step is None or not step.worker_step_ref:
        return None
    sub = wdef.find_sub(step.worker_step_ref)
    if sub is None or not sub.inputs:
        return None

    values, missing = resolve_inputs(sub.inputs, provided, attachment_names)
    return StepInputResolution(
        step_id=step.id,
        step_name=step.name,
        sub_ref=sub.ref,
        specs=list(sub.inputs),
        values=values,
        missing=missing,
    )


async def current_step_id(run: Run, *, db: AsyncSession | None = None) -> str | None:
    """本 run 当前子任务的 id（**不要求该子任务声明输入**）。

    产物归属（P0-5 收尾）要知道材料是挂在哪个子任务上产出的，这与"那个子任务缺不缺输入"
    是两件事：没有输入声明的子任务照样产出材料，归属不该因此丢掉。取步口径与
    `resolve_step_inputs` 一致（同一个"当前子任务"），只在没拿到预检结果时兜底调用。
    """
    payload = run.input if isinstance(run.input, Mapping) else {}
    task_id = payload.get("task_id")
    if not task_id:
        return None
    try:
        tid = UUID(str(task_id))
    except (TypeError, ValueError):
        return None
    if db is not None:
        step = await _current_step(db, tid)
    else:
        async with session_factory() as own:
            step = await _current_step(own, tid)
    return str(step.id) if step is not None else None


async def _collect_step(
    task_id: UUID, run: Run, db: AsyncSession
) -> tuple[TaskStep | None, dict[str, str], list[str]]:
    """当前子任务 + 本会话可确定性拿到的取值与附件名。"""
    step = await _current_step(db, task_id)
    provided, attachment_names = await _collect(run, db)
    return step, provided, attachment_names


async def _current_step(db: AsyncSession, task_id: UUID) -> TaskStep | None:
    """第一个未收口的主线步骤：doing 优先，否则 seq 最小的 pending。"""
    rows = list(
        (
            await db.scalars(
                select(TaskStep)
                .where(
                    TaskStep.task_id == task_id,
                    TaskStep.kind == "main",
                    TaskStep.status.in_(("doing", "pending")),
                )
                .order_by(TaskStep.seq.asc())
            )
        ).all()
    )
    if not rows:
        return None
    doing = [s for s in rows if s.status == "doing"]
    return doing[0] if doing else rows[0]


async def _collect(run: Run, db: AsyncSession) -> tuple[dict[str, str], list[str]]:
    """汇总会话内可确定性拿到的取值与附件名（时间升序，后者覆盖前者）。"""
    provided: dict[str, str] = {}
    attachment_ids: list[UUID] = []
    for snap in await _session_snapshots(run, db):
        raw = snap.get("inputs")
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    provided[str(key)] = text
        for fid in snap.get("attachment_ids") or []:
            try:
                pid = UUID(str(fid))
            except (TypeError, ValueError):
                continue
            if pid not in attachment_ids:
                attachment_ids.append(pid)
    return provided, await _attachment_names(db, attachment_ids)


async def _session_snapshots(run: Run, db: AsyncSession) -> list[Mapping[str, Any]]:
    """本会话截至本 run 的 input 快照（含本 run），按时间升序。"""
    own = run.input if isinstance(run.input, Mapping) else {}
    if run.conversation_id is None or run.created_at is None:
        return [own]
    stmt = (
        select(Run.input)
        .where(
            Run.conversation_id == run.conversation_id,
            Run.created_at <= run.created_at,
        )
        .order_by(Run.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    snaps = [row for row in rows if isinstance(row, Mapping)]
    # own 兜底追加：run 尚未 flush 进查询结果时也不能丢自己的取值（重复合并无害）
    snaps.append(own)
    return snaps


async def _attachment_names(db: AsyncSession, ids: list[UUID]) -> list[str]:
    """附件 id（按会话顺序）→ 文件名；已被删除的附件跳过（不阻断其余输入）。"""
    if not ids:
        return []
    rows = (await db.execute(select(File.id, File.filename).where(File.id.in_(ids)))).all()
    by_id = {row[0]: str(row[1]) for row in rows}
    return [by_id[fid] for fid in ids if fid in by_id]
