"""runs 路由：任务视图 + 控制面（abort）+ SSE 事件流（含 seq 断线续传）。

SSE 协议（模块详细设计 §2.3）：
- 历史段：run_events 表 seq > after 补齐
- 实时段：LISTEN run_events 频道，按 run_id 过滤
- 断线重连：客户端回传 Last-Event-ID（或 after 查询参数）
- 终态事件（run_status done/failed/cancelled）送达后流结束
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.runs.models import Run
from app.modules.runs.schemas import ConfirmIn, RunEventOut, RunOut

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/runs",
    tags=["runs"],
    dependencies=[Depends(get_current_user)],
)

TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _pg_dsn() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@router.get("", response_model=list[RunOut])
async def list_runs(
    conversation_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> list[Run]:
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if conversation_id:
        stmt = stmt.where(Run.conversation_id == conversation_id)
    if status_filter:
        stmt = stmt.where(Run.status == status_filter)
    return list((await db.scalars(stmt)).all())


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: UUID, db: AsyncSession = Depends(get_db)) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return run


@router.get("/{run_id}/events", response_model=list[RunEventOut])
async def list_run_events(
    run_id: UUID,
    after: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[RunEventOut]:
    """事件历史（非流式版，调试与前端初始化补齐用）。"""
    stmt = (
        text(
            "SELECT id, run_id, seq, event_type, payload, created_at FROM run_events "
            "WHERE run_id = :rid AND seq > :after ORDER BY seq"
        )
    ).bindparams(rid=run_id, after=after)
    result = await db.execute(stmt)
    rows = result.mappings().all()
    return [RunEventOut(**dict(r)) for r in rows]  # type: ignore[arg-type]


@router.post("/{run_id}/abort", status_code=status.HTTP_202_ACCEPTED)
async def abort_run(run_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """控制面 = 投 inbox 事件（模块详细设计 §2.3），不持有执行态。"""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if run.status in TERMINAL_STATUSES:
        return {"run_id": str(run.id), "status": run.status, "detail": "已是终态"}
    await db.execute(
        text(
            "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
            "VALUES ('abort', :rid, '{}'::jsonb, 'new')"
        ),
        {"rid": run.id},
    )
    await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
    await db.commit()
    return {"run_id": str(run.id), "status": "aborting"}


@router.post("/{run_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_run(run_id: UUID, body: ConfirmIn, db: AsyncSession = Depends(get_db)) -> dict:
    """确认卡片提交：投 confirmation 事件，引擎从 interrupt 检查点恢复。"""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if run.status != "paused_awaiting_confirm":
        raise HTTPException(status.HTTP_409_CONFLICT, f"任务不在待确认状态（当前 {run.status}）")
    # 统一结构化回传（§3.5）：data/applied 仅在存在时随 answer 一并投给引擎
    inbox_payload: dict[str, Any] = {"answer": body.answer}
    if body.data is not None:
        inbox_payload["data"] = body.data
    if body.applied is not None:
        inbox_payload["applied"] = body.applied
    await db.execute(
        text(
            "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
            "VALUES ('confirmation', :rid, CAST(:p AS jsonb), 'new')"
        ),
        {"rid": run.id, "p": json.dumps(inbox_payload, ensure_ascii=False)},
    )
    await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
    await db.commit()
    # 纠错沉淀（M6，模块详细设计 §1.4）：驳回计划的 run 异步触发复盘——
    # 把"别这么做"写进草稿的注意事项区。fire-and-forget，不阻塞确认响应。
    if body.answer == "rejected":
        import asyncio

        from app.modules.skills_forge.service import review_run

        asyncio.create_task(review_run(str(run.id), trigger="correction"))
    return {"run_id": str(run.id), "status": "resuming", "answer": body.answer}


def _sse_chunk(seq: int, event_type: str, payload: dict) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/{run_id}/stream")
async def stream_run_events(
    run_id: UUID,
    after: int = Query(default=0, description="断线续传游标（上次收到的 seq）"),
) -> StreamingResponse:
    """SSE 事件流：历史补齐 + 实时推送，终态后自然结束。"""
    rid = str(run_id)

    async def gen() -> AsyncIterator[str]:
        last_seq = after
        conn = await asyncpg.connect(_pg_dsn())
        notify_q: asyncio.Queue[str] = asyncio.Queue()

        def _on_notify(*args: object) -> None:
            payload = str(args[3]) if len(args) > 3 else ""
            notify_q.put_nowait(payload)

        await conn.add_listener("run_events", _on_notify)
        try:
            while True:
                rows = await conn.fetch(
                    "SELECT seq, event_type, payload FROM run_events "
                    "WHERE run_id = $1 AND seq > $2 ORDER BY seq",
                    rid,
                    last_seq,
                )
                terminal = False
                for r in rows:
                    last_seq = r["seq"]
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    yield _sse_chunk(r["seq"], r["event_type"], payload)
                    if (
                        r["event_type"] == "run_status"
                        and payload.get("status") in TERMINAL_STATUSES
                    ):
                        terminal = True
                if terminal:
                    return
                # 等 NOTIFY 或 25s 心跳
                try:
                    notified = await asyncio.wait_for(notify_q.get(), timeout=25)
                    if notified != rid:  # 其它 run 的事件，继续轮询本轮
                        continue
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            await conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
