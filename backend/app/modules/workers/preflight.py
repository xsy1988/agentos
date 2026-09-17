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
from app.modules.workers import registry
from app.modules.workers.inputs import (
    InputSpec,
    missing_inputs_text,
    render_input_contract,
    resolve_inputs,
)

logger = logging.getLogger(__name__)


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
