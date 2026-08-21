from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from swingscan.market_clock import (
    add_trading_days,
    is_session_open,
    is_trading_day,
    last_completed_session,
    next_trading_day,
    previous_trading_day,
    session_state,
)

ALMATY = ZoneInfo("Asia/Almaty")


def _almaty(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ALMATY)


def test_weekend_and_holidays_are_not_trading_days():
    assert is_trading_day(date(2026, 8, 21)) is True  # пятница
    assert is_trading_day(date(2026, 8, 22)) is False  # суббота
    assert is_trading_day(date(2026, 7, 3)) is False  # перенос Дня независимости
    assert is_trading_day(date(2026, 12, 25)) is False  # Рождество


def test_previous_and_next_trading_day_skip_weekend():
    assert previous_trading_day(date(2026, 8, 24)) == date(2026, 8, 21)
    assert next_trading_day(date(2026, 8, 21)) == date(2026, 8, 24)


def test_add_trading_days_matches_three_week_horizon():
    # 15 торговых сессий ≈ 3 календарные недели
    assert add_trading_days(date(2026, 8, 20), 15) == date(2026, 9, 11)
    assert add_trading_days(date(2026, 8, 20), 0) == date(2026, 8, 20)


def test_morning_scan_runs_after_us_close():
    """09:00 Астаны = 04:00 UTC = 00:00 в Нью-Йорке — рынок закрыт."""
    moment = _almaty("2026-08-21 09:00")
    assert session_state(moment) == "premarket"
    assert is_session_open(moment) is False
    assert last_completed_session(moment) == date(2026, 8, 20)


def test_evening_scan_runs_mid_session():
    """21:00 Астаны = 16:00 UTC = 12:00 в Нью-Йорке — торги идут."""
    moment = _almaty("2026-08-21 21:00")
    assert session_state(moment) == "open"
    assert is_session_open(moment) is True
    assert last_completed_session(moment) == date(2026, 8, 20)


def test_saturday_morning_scan_sees_friday_close():
    moment = _almaty("2026-08-22 09:00")
    assert session_state(moment) == "holiday"
    assert last_completed_session(moment) == date(2026, 8, 21)


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-08-21 18:00", "premarket"),  # 13:00 UTC -> 09:00 ET, до открытия
        ("2026-08-21 18:35", "open"),  # 13:35 UTC -> 09:35 ET, торги начались
        ("2026-08-22 03:00", "closed"),  # 22:00 UTC пт -> 18:00 ET пт
    ],
)
def test_session_state_edges(moment, expected):
    assert session_state(_almaty(moment)) == expected
