"""Состояние торговой сессии США — чтобы понимать, полные ли данные последнего бара.

Сканирование в 09:00 по Астане (04:00 UTC) приходится на время, когда рынок США
уже закрыт: последний дневной бар полный. Сканирование в 21:00 по Астане
(16:00 UTC) попадает в середину американской сессии — последний бар ещё
формируется. В этом случае сигнал помечается как предварительный.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Праздники NYSE/NASDAQ (полные выходные). Обновляемый список.
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
        date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
        date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
        # 2028
        date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
        date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
        date(2028, 12, 25),
    }
)


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def add_trading_days(day: date, count: int) -> date:
    cursor = day
    for _ in range(max(0, count)):
        cursor = next_trading_day(cursor)
    return cursor


def session_state(moment: datetime) -> str:
    """'closed' | 'open' | 'premarket' | 'holiday' для момента времени."""
    local = moment.astimezone(NY)
    if not is_trading_day(local.date()):
        return "holiday"
    if local.time() < MARKET_OPEN:
        return "premarket"
    if local.time() >= MARKET_CLOSE:
        return "closed"
    return "open"


def is_session_open(moment: datetime) -> bool:
    return session_state(moment) == "open"


def last_completed_session(moment: datetime) -> date:
    """Дата последней полностью закрытой торговой сессии США."""
    local = moment.astimezone(NY)
    today = local.date()
    if is_trading_day(today) and local.time() >= MARKET_CLOSE:
        return today
    return previous_trading_day(today)
