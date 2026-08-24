"""scheduler 服务：timer 到点创建 run（与手动触发同一条 inbox 路）。"""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from app.core.db import session_factory
from app.modules.agents.models import Agent
from app.modules.runs.models import Run

logger = logging.getLogger(__name__)


def render_input(template: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """input_template 实例化：支持 {date} {time} {now} 简单变量。"""
    now = now or datetime.now(UTC)
    out = dict(template or {})
    for key, val in out.items():
        if isinstance(val, str):
            out[key] = val.format(
                date=now.strftime("%Y-%m-%d"),
                time=now.strftime("%H:%M"),
                now=now.strftime("%Y-%m-%d %H:%M"),
            )
    return out


async def fire_timer(timer_id: uuid.UUID) -> str | None:
    """timer 到点：实例化输入 → 创建 run(pending, trigger=timer) → 投 inbox + NOTIFY。

    与手动触发同一条路（conversations.send_message 的 run 创建纪律）；
    无会话归属，thread_id 由 runtime 用 run_id 兜底。
    """
    from app.modules.scheduler.models import Timer

    async with session_factory() as db:
        timer = await db.get(Timer, timer_id)
        if timer is None or timer.status != "active":
            return None
        agent = (
            await db.execute(
                select(Agent).where(Agent.id == timer.agent_id, Agent.status == "enabled")
            )
        ).scalar_one_or_none()
        if agent is None:
            logger.warning("timer %s 的 Agent 不可用，跳过触发", timer_id)
            return None
        now = datetime.now(UTC)
        input_payload = render_input(timer.input_template or {}, now)
        run = Run(
            conversation_id=None,
            agent_id=timer.agent_id,
            trigger="timer",
            input=input_payload,
            # 预算快照创建时固化（数据库设计 §2.3，与手动触发同纪律）
            budget={
                "tool_budget": agent.tool_budget,
                "max_iterations": agent.max_iterations,
                "max_tokens_per_run": agent.max_tokens_per_run,
                "timeout_seconds": agent.timeout_seconds,
            },
        )
        db.add(run)
        await db.flush()
        await db.execute(
            text(
                "INSERT INTO inbox_events (event_type, target_run_id, payload, status) "
                "VALUES ('user_input', :rid, CAST(:p AS jsonb), 'new')"
            ),
            {"rid": run.id, "p": json.dumps({"run_id": str(run.id)}, ensure_ascii=False)},
        )
        await db.execute(text("SELECT pg_notify('inbox_events', :rid)"), {"rid": str(run.id)})
        timer.last_fire_at = now
        await db.commit()
        return str(run.id)


async def fire_alarm(alarm_id: uuid.UUID) -> None:
    """alarm 到点：纯提醒，写通知中心（不建 run）。"""
    from app.modules.notifications.models import Notification
    from app.modules.scheduler.models import Alarm

    async with session_factory() as db:
        alarm = await db.get(Alarm, alarm_id)
        if alarm is None or alarm.status != "waiting":
            return
        db.add(
            Notification(
                kind="alarm", title="闹钟提醒", content=alarm.content, url=alarm.url
            )
        )
        alarm.status = "fired"
        await db.commit()
