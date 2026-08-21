from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from swingscan.config import Settings
from swingscan.scheduler import (
    MISFIRE_GRACE_SECONDS,
    build_triggers,
    create_scheduler,
    next_run_times,
)

ALMATY = ZoneInfo("Asia/Almaty")
UTC = ZoneInfo("UTC")


def test_two_triggers_are_created():
    assert len(build_triggers(Settings())) == 2


def test_next_runs_are_at_9_and_21_almaty():
    after = datetime(2026, 8, 21, 10, 0, tzinfo=ALMATY)
    runs = next_run_times(Settings(), after)
    assert [(r.hour, r.minute) for r in runs] == [(9, 0), (21, 0)]
    assert all(str(r.tzinfo) == "Asia/Almaty" for r in runs)


def test_scan_times_map_to_expected_utc_hours():
    """09:00 Астаны = 04:00 UTC, 21:00 Астаны = 16:00 UTC (Казахстан — UTC+5)."""
    after = datetime(2026, 8, 21, 10, 0, tzinfo=ALMATY)
    utc_hours = sorted(r.astimezone(UTC).hour for r in next_run_times(Settings(), after))
    assert utc_hours == [4, 16]


def test_utc_offset_is_five_hours_year_round():
    """В Казахстане нет перехода на летнее время — проверяем зиму и лето."""
    for moment in (datetime(2026, 1, 15, 9, tzinfo=ALMATY), datetime(2026, 7, 15, 9, tzinfo=ALMATY)):
        assert moment.utcoffset().total_seconds() == 5 * 3600


def test_custom_scan_times_are_respected():
    settings = Settings().with_overrides(scan_times=("07:30", "12:15", "23:45"))
    runs = next_run_times(settings, datetime(2026, 8, 21, 0, 0, tzinfo=ALMATY))
    assert [(r.hour, r.minute) for r in runs] == [(7, 30), (12, 15), (23, 45)]


def test_invalid_scan_time_raises():
    settings = Settings().with_overrides(scan_times=("девять",))
    with pytest.raises(ValueError):
        build_triggers(settings)


def test_scheduler_registers_jobs_with_misfire_grace():
    calls: list[int] = []
    scheduler = create_scheduler(Settings(), job=lambda: calls.append(1), blocking=False)
    jobs = scheduler.get_jobs()
    assert len(jobs) == 2
    assert {job.id for job in jobs} == {"scan-0900", "scan-2100"}
    for job in jobs:
        assert job.misfire_grace_time == MISFIRE_GRACE_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1


def test_scheduler_job_is_callable():
    calls: list[str] = []
    scheduler = create_scheduler(Settings(), job=lambda: calls.append("ran"), blocking=False)
    scheduler.get_jobs()[0].func()
    assert calls == ["ran"]


def test_saturday_is_scheduled_to_catch_friday_close():
    """Пятничное закрытие США приходится на субботу по Астане."""
    friday_evening = datetime(2026, 8, 21, 23, 0, tzinfo=ALMATY)
    morning_trigger = build_triggers(Settings())[0]
    next_fire = morning_trigger.get_next_fire_time(None, friday_evening)
    assert next_fire.weekday() == 5  # суббота


def test_guarded_run_swallows_errors(monkeypatch):
    from swingscan import scheduler as scheduler_module

    def boom(_settings):
        raise RuntimeError("Yahoo лёг")

    monkeypatch.setattr(scheduler_module, "run_once", boom)
    scheduler_module._guarded_run(Settings())  # не должно бросать исключение


def test_serve_can_run_immediately_and_starts_scheduler(monkeypatch, tmp_path):
    from swingscan import scheduler as scheduler_module

    runs: list[str] = []
    started: list[str] = []

    class FakeScheduler:
        def start(self):
            started.append("start")

    monkeypatch.setattr(scheduler_module, "run_once", lambda _s: runs.append("scan"))
    monkeypatch.setattr(scheduler_module, "create_scheduler", lambda _s: FakeScheduler())

    settings = Settings().with_overrides(
        state_dir=tmp_path / "state", reports_dir=tmp_path / "reports"
    )
    scheduler_module.serve(settings, run_immediately=True)
    assert runs == ["scan"]
    assert started == ["start"]
