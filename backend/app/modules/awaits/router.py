"""awaits 路由（用户面）：等待清单 + 人工撤销。回调入口在 open_api（外部面）。

端点一览：
- GET  /awaits?run_id=&status=     等待清单（谁在等、等多久、超时点）
- POST /awaits/{await_id}/cancel   撤销等待（引擎按结构化失败 await_cancelled 继续）
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.auth.deps import get_current_user
from app.modules.awaits.models import AWAIT_STATUSES, AwaitBroker
from app.modules.awaits.schemas import AwaitCancelOut, AwaitListOut, AwaitOut
from app.modules.awaits.service import brief, cancel, enqueue_resume, list_awaits

router = APIRouter(
    prefix="/awaits",
    tags=["awaits"],
    dependencies=[Depends(get_current_user)],
)


@router.get("", response_model=AwaitListOut)
async def get_awaits(
    run_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> AwaitListOut:
    if status_filter is not None and status_filter not in AWAIT_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"非法状态：{status_filter}（可选 {', '.join(AWAIT_STATUSES)}）",
        )
    rows = await list_awaits(db, run_id=run_id, status=status_filter, limit=limit)
    return AwaitListOut(items=[AwaitOut(**brief(r)) for r in rows])


@router.post("/{await_id}/cancel", response_model=AwaitCancelOut)
async def cancel_await(await_id: UUID, db: AsyncSession = Depends(get_db)) -> AwaitCancelOut:
    """撤销等待：CAS waiting→cancelled 成功后唤醒 run（注入结构化失败，不静默）。"""
    row = await db.get(AwaitBroker, await_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "等待记录不存在")
    if row.status != "waiting":
        return AwaitCancelOut(
            await_id=str(row.id), run_id=str(row.run_id), status=row.status, cancelled=False
        )
    won = await cancel(db, row, reason="用户撤销等待")
    if won:
        await enqueue_resume(db, row, status="cancelled")
    await db.commit()
    return AwaitCancelOut(
        await_id=str(row.id), run_id=str(row.run_id), status=row.status, cancelled=won
    )
