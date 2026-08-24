"""scheduler 路由：timers / alarms CRUD（变更后热重载调度器）。"""

import uuid
from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.modules.agents.models import Agent
from app.modules.auth.deps import get_current_user
from app.modules.scheduler.models import Alarm, Timer
from app.modules.scheduler.runtime import scheduler_runtime

router = APIRouter(
    prefix="/scheduler",
    tags=["scheduler"],
    dependencies=[Depends(get_current_user)],
)


class TimerCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron_expr: str = Field(min_length=5, max_length=64)
    agent_id: str
    input_template: dict[str, Any] = {}


class TimerUpdateIn(BaseModel):
    name: str | None = None
    cron_expr: str | None = None
    input_template: dict[str, Any] | None = None
    status: str | None = None  # active / paused


class TimerOut(BaseModel):
    id: str
    name: str
    cron_expr: str
    agent_id: str
    input_template: dict[str, Any]
    next_fire_at: str | None
    last_fire_at: str | None
    status: str


class AlarmCreateIn(BaseModel):
    content: str = Field(min_length=1)
    fire_at: datetime
    url: str | None = None


class AlarmOut(BaseModel):
    id: str
    content: str
    fire_at: str
    url: str | None
    status: str


def _timer_out(t: Timer) -> TimerOut:
    return TimerOut(
        id=str(t.id),
        name=t.name,
        cron_expr=t.cron_expr,
        agent_id=str(t.agent_id),
        input_template=t.input_template or {},
        next_fire_at=t.next_fire_at.isoformat() if t.next_fire_at else None,
        last_fire_at=t.last_fire_at.isoformat() if t.last_fire_at else None,
        status=t.status,
    )


def _alarm_out(a: Alarm) -> AlarmOut:
    return AlarmOut(
        id=str(a.id),
        content=a.content,
        fire_at=a.fire_at.isoformat(),
        url=a.url,
        status=a.status,
    )


async def _reload_timers() -> None:
    if scheduler_runtime.running:
        await scheduler_runtime.reload_timers()


async def _reload_alarms() -> None:
    if scheduler_runtime.running:
        await scheduler_runtime.reload_alarms()


@router.get("/timers", response_model=list[TimerOut])
async def list_timers(db: AsyncSession = Depends(get_db)) -> list[TimerOut]:
    timers = (await db.execute(select(Timer).order_by(Timer.created_at))).scalars().all()
    return [_timer_out(t) for t in timers]


@router.post("/timers", response_model=TimerOut, status_code=201)
async def create_timer(body: TimerCreateIn, db: AsyncSession = Depends(get_db)) -> TimerOut:
    try:
        CronTrigger.from_crontab(body.cron_expr)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"cron 表达式非法：{exc}") from exc
    agent = await db.get(Agent, uuid.UUID(body.agent_id))
    if agent is None or agent.status != "enabled":
        raise HTTPException(status_code=409, detail="Agent 不存在或不可用")
    timer = Timer(
        name=body.name,
        cron_expr=body.cron_expr,
        agent_id=agent.id,
        input_template=body.input_template,
    )
    db.add(timer)
    await db.commit()
    await db.refresh(timer)
    await _reload_timers()
    await db.refresh(timer)
    return _timer_out(timer)


@router.patch("/timers/{timer_id}", response_model=TimerOut)
async def update_timer(
    timer_id: uuid.UUID, body: TimerUpdateIn, db: AsyncSession = Depends(get_db)
) -> TimerOut:
    timer = await db.get(Timer, timer_id)
    if timer is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    if body.cron_expr is not None and body.cron_expr != timer.cron_expr:
        try:
            CronTrigger.from_crontab(body.cron_expr)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"cron 表达式非法：{exc}") from exc
        timer.cron_expr = body.cron_expr
    if body.name is not None:
        timer.name = body.name
    if body.input_template is not None:
        timer.input_template = body.input_template
    if body.status is not None:
        if body.status not in ("active", "paused"):
            raise HTTPException(status_code=422, detail="status 只能是 active/paused")
        timer.status = body.status
    await db.commit()
    await _reload_timers()
    await db.refresh(timer)
    return _timer_out(timer)


@router.delete("/timers/{timer_id}", status_code=204)
async def delete_timer(timer_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    timer = await db.get(Timer, timer_id)
    if timer is None:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    await db.delete(timer)
    await db.commit()
    await _reload_timers()


@router.get("/alarms", response_model=list[AlarmOut])
async def list_alarms(db: AsyncSession = Depends(get_db)) -> list[AlarmOut]:
    alarms = (await db.execute(select(Alarm).order_by(Alarm.fire_at))).scalars().all()
    return [_alarm_out(a) for a in alarms]


@router.post("/alarms", response_model=AlarmOut, status_code=201)
async def create_alarm(body: AlarmCreateIn, db: AsyncSession = Depends(get_db)) -> AlarmOut:
    alarm = Alarm(content=body.content, fire_at=body.fire_at, url=body.url)
    db.add(alarm)
    await db.commit()
    await db.refresh(alarm)
    await _reload_alarms()
    return _alarm_out(alarm)


@router.delete("/alarms/{alarm_id}", status_code=204)
async def cancel_alarm(alarm_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    alarm = await db.get(Alarm, alarm_id)
    if alarm is None:
        raise HTTPException(status_code=404, detail="闹钟不存在")
    alarm.status = "cancelled"
    await db.commit()
    await _reload_alarms()
