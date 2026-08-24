"""scheduler 运行时：APScheduler（AsyncIOScheduler）装配三类 Job。

timers/alarms 表为唯一事实源，进程重启全量重载（模块详细设计 §1.5）：
- timer：cron 到点 → fire_timer 创建 run + inbox NOTIFY（与手动触发同一条路）
- alarm：绝对时间到点 → fire_alarm 写通知中心（纯提醒）
- memory_consolidation：每日 03:00 → memory.consolidate_daily()
"""

import logging
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from app.core.db import session_factory

logger = logging.getLogger(__name__)

CONSOLIDATE_CRON_HOUR = 3  # 每日整理时刻（模块详细设计 §2.6）


def _timer_job_id(timer_id: str) -> str:
    return f"timer-{timer_id}"


def _alarm_job_id(alarm_id: str) -> str:
    return f"alarm-{alarm_id}"


class SchedulerRuntime:
    """进程内单例：持有 AsyncIOScheduler，CRUD 后调 reload_timers/reload_alarms。"""

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        if self.running:
            return
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        # 每日记忆整理：03:00
        self._scheduler.add_job(
            self._memory_consolidation,
            CronTrigger(hour=CONSOLIDATE_CRON_HOUR, minute=0, timezone="UTC"),
            id="memory_consolidation",
            replace_existing=True,
        )
        self._scheduler.start()
        await self.reload_timers()
        await self.reload_alarms()
        logger.info("SchedulerRuntime started (memory_consolidation @ 03:00 UTC)")

    async def stop(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self._scheduler = None

    # ---------- 重载（表为唯一事实源） ----------

    async def reload_timers(self) -> None:
        """全量重载 active timers 为 cron job（暂停/失效的不注册）。"""
        from app.modules.scheduler.models import Timer

        if self._scheduler is None:
            return
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("timer-"):
                self._scheduler.remove_job(job.id)
        async with session_factory() as db:
            timers = (
                await db.execute(select(Timer).where(Timer.status == "active"))
            ).scalars().all()
        for t in timers:
            try:
                trigger = CronTrigger.from_crontab(t.cron_expr, timezone="UTC")
            except ValueError as exc:
                logger.warning("timer %s cron 非法（%s），跳过", t.id, exc)
                continue
            self._scheduler.add_job(
                self._fire_timer,
                trigger,
                id=_timer_job_id(str(t.id)),
                args=[str(t.id)],
                replace_existing=True,
            )
            # next_fire_at 回写表（管理端展示）
            job = self._scheduler.get_job(_timer_job_id(str(t.id)))
            if job is not None and job.next_run_time is not None:
                from sqlalchemy import update

                async with session_factory() as db:
                    await db.execute(
                        update(Timer)
                        .where(Timer.id == t.id)
                        .values(next_fire_at=job.next_run_time)
                    )
                    await db.commit()

    async def reload_alarms(self) -> None:
        """全量重载 waiting alarms 为一次性 date job。"""
        from app.modules.scheduler.models import Alarm

        if self._scheduler is None:
            return
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("alarm-"):
                self._scheduler.remove_job(job.id)
        async with session_factory() as db:
            alarms = (
                await db.execute(select(Alarm).where(Alarm.status == "waiting"))
            ).scalars().all()
        for a in alarms:
            self._scheduler.add_job(
                self._fire_alarm,
                DateTrigger(run_date=a.fire_at, timezone="UTC"),
                id=_alarm_job_id(str(a.id)),
                args=[str(a.id)],
                replace_existing=True,
            )

    # ---------- Job 回调 ----------

    async def _fire_timer(self, timer_id: str) -> None:
        from app.modules.scheduler import service

        try:
            run_id = await service.fire_timer(uuid.UUID(timer_id))
            if run_id:
                logger.info("timer %s fired → run %s", timer_id, run_id)
            await self.reload_timers()  # 刷新 next_fire_at
        except Exception:  # noqa: BLE001 Job 永不冒泡
            logger.exception("timer %s fire failed", timer_id)

    async def _fire_alarm(self, alarm_id: str) -> None:
        from app.modules.scheduler import service

        try:
            await service.fire_alarm(uuid.UUID(alarm_id))
            logger.info("alarm %s fired", alarm_id)
        except Exception:  # noqa: BLE001
            logger.exception("alarm %s fire failed", alarm_id)

    async def _memory_consolidation(self) -> None:
        from app.modules.memory import service as memory_service

        try:
            result = await memory_service.consolidate_daily()
            logger.info("memory consolidation: %s", result)
        except Exception:  # noqa: BLE001
            logger.exception("memory consolidation failed")


scheduler_runtime = SchedulerRuntime()
