"""Расписание: два скана в сутки по времени Астаны (по умолчанию 09:00 и 21:00).

Казахстан с 01.03.2024 в едином поясе UTC+5 без перехода на летнее время, но
триггеры всё равно строятся с явной таймзоной — если правила изменятся,
приложение продолжит срабатывать в правильное локальное время.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .app import run_once
from .config import Settings, parse_hhmm

log = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 30 * 60


def build_triggers(settings: Settings) -> list[CronTrigger]:
    tz = ZoneInfo(settings.timezone)
    triggers = []
    for value in settings.scan_times:
        hour, minute = parse_hhmm(value)
        triggers.append(
            CronTrigger(
                day_of_week="mon-sat",  # сб — чтобы поймать пятничное закрытие США
                hour=hour,
                minute=minute,
                timezone=tz,
            )
        )
    return triggers


def next_run_times(settings: Settings, after: datetime | None = None) -> list[datetime]:
    tz = ZoneInfo(settings.timezone)
    moment = (after or datetime.now(tz)).astimezone(tz)
    return [trigger.get_next_fire_time(None, moment) for trigger in build_triggers(settings)]


def create_scheduler(
    settings: Settings,
    job: Callable[[], None] | None = None,
    *,
    blocking: bool = True,
) -> BlockingScheduler | BackgroundScheduler:
    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_cls(timezone=ZoneInfo(settings.timezone))
    task = job or (lambda: _guarded_run(settings))
    for index, trigger in enumerate(build_triggers(settings)):
        scheduler.add_job(
            task,
            trigger=trigger,
            id=f"scan-{settings.scan_times[index].replace(':', '')}",
            name=f"Скан рынка США в {settings.scan_times[index]} ({settings.timezone})",
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
        )
    return scheduler


def _guarded_run(settings: Settings) -> None:
    """Ошибка одного запуска не должна убивать планировщик."""
    try:
        run_once(settings)
    except Exception:  # noqa: BLE001
        log.exception("Сканирование завершилось ошибкой")


def serve(settings: Settings, *, run_immediately: bool = False) -> None:
    settings.ensure_dirs()
    scheduler = create_scheduler(settings)
    times = ", ".join(settings.scan_times)
    log.info("Планировщик запущен: %s (%s)", times, settings.timezone)
    for moment in next_run_times(settings):
        if moment:
            log.info("Ближайший запуск: %s", moment.strftime("%d.%m.%Y %H:%M %Z"))
    if run_immediately:
        _guarded_run(settings)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        log.info("Остановка планировщика")
        scheduler.shutdown(wait=False)
