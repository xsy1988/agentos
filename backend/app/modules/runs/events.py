"""run_events 的唯一写入实现（模块详细设计 §2.3）。

seq 分配与 `pg_notify` 只在这里实现一次：engine 的实时通道（SSE）与任务侧的
状态收敛事件都必须复用，两处各写一份一旦口径漂移，SSE 断线续传的 seq 就会撕裂。

边界说明：run_events 仍归 runs 模块所有，engine 只是调用方；tasks 侧发出的是
**关于本 run 的事实**（支线收敛），不触发 run、不改 run 状态。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def emit_event(
    run_id: str | uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    *,
    db: AsyncSession,
) -> int:
    """向 run 的事件流追加一条（seq = 该 run 内 max+1）+ NOTIFY，返回 seq。

    **不提交事务**：调用方必须把它与自身业务写放进同一事务，否则会出现
    「状态改了但事件没发」或反之的撕裂（收敛语义尤其不能丢事件）。
    """
    rid = str(run_id)
    result = await db.execute(
        text(
            "INSERT INTO run_events (run_id, seq, event_type, payload) "
            "SELECT :rid, COALESCE(MAX(seq), 0) + 1, :et, CAST(:p AS jsonb) "
            "FROM run_events WHERE run_id = :rid RETURNING seq"
        ),
        {"rid": rid, "et": event_type, "p": json.dumps(payload, ensure_ascii=False)},
    )
    seq = int(result.scalar_one())
    await db.execute(text("SELECT pg_notify('run_events', :rid)"), {"rid": rid})
    return seq
