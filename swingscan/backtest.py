"""Проверка сетапа на истории самой бумаги.

Симулируется ровно то правило, по которому выдаётся живой сигнал:

* вход — стоп-лимит по цене ``entry``; ордер живёт ``entry_valid_bars`` сессий,
  если цена не дошла — сигнал аннулируется (сделки не было);
* стоп — ``max_risk_pct`` от входа (или структурный, если он туже);
* цель — ``min_target_pct`` от входа;
* выход по времени — на закрытии ``max_hold_days``-го бара.

Внутри бара порядок неизвестен, поэтому при одновременном касании стопа и цели
результат считается убыточным (консервативная оценка). Гэпы исполняются по
цене открытия, поэтому убыток может превысить 1R — это честно отражается в
матожидании.
"""

from __future__ import annotations

import math

import numpy as np

from .config import TradeSpec
from .models import BacktestStats
from .strategy import (
    PreparedFrame,
    build_plan,
    check_feasibility,
    check_risk_reward,
    detect_setup,
)

ENTRY_VALID_BARS = 3
WARMUP_BARS = 60


def _simulate_trade(
    data: PreparedFrame,
    fill_index: int,
    entry: float,
    stop: float,
    target: float,
    risk_per_share: float,
    max_hold: int,
) -> tuple[float, int, str]:
    """Возвращает (результат в R, сколько баров держали, исход)."""
    high = data.array("High")
    low = data.array("Low")
    close = data.array("Close")
    open_ = data.array("Open")
    last = min(len(data) - 1, fill_index + max_hold - 1)

    for j in range(fill_index, last + 1):
        # Гэп вниз ниже стопа — исполнение по открытию.
        if j > fill_index and open_[j] <= stop:
            return (float(open_[j]) - entry) / risk_per_share, j - fill_index + 1, "loss"
        # Гэп вверх выше цели — фиксация по открытию.
        if j > fill_index and open_[j] >= target:
            return (float(open_[j]) - entry) / risk_per_share, j - fill_index + 1, "win"
        touched_stop = low[j] <= stop
        touched_target = high[j] >= target
        if touched_stop:  # консервативно: стоп приоритетнее цели
            return -1.0, j - fill_index + 1, "loss"
        if touched_target:
            return (target - entry) / risk_per_share, j - fill_index + 1, "win"

    exit_price = float(close[last])
    return (exit_price - entry) / risk_per_share, last - fill_index + 1, "timeout"


def backtest_symbol(
    data: PreparedFrame,
    spec: TradeSpec,
    *,
    lookback_bars: int = 504,
    skip_last: int = 0,
) -> BacktestStats:
    """Прогоняет правило по последним ``lookback_bars`` барам истории.

    ``skip_last`` исключает хвост истории (например, текущий сигнал), чтобы
    статистика не включала ещё не завершённую сделку.
    """
    n = len(data)
    stats = BacktestStats()
    if n < WARMUP_BARS + spec.max_hold_days + 5:
        return stats

    end = n - skip_last - spec.max_hold_days - 1
    start = max(WARMUP_BARS, n - lookback_bars)
    if end <= start:
        return stats

    high = data.array("High")
    bars_held: list[int] = []
    results: list[float] = []

    i = start
    while i < end:
        signal = detect_setup(data, i)
        if signal is None:
            i += 1
            continue

        plan = build_plan(signal, spec)
        if not check_risk_reward(plan, spec).ok:
            i += 1
            continue
        recent_low = float(np.min(data.array("Low")[max(0, i - 1) : i + 1]))
        if not check_feasibility(plan, data.get("atr_pct", i), recent_low, spec).ok:
            i += 1
            continue

        # Ищем бар, на котором сработал стоп-ордер на вход.
        fill_index = -1
        for j in range(i + 1, min(n, i + 1 + ENTRY_VALID_BARS)):
            if high[j] >= plan.entry:
                fill_index = j
                break
        if fill_index < 0:
            i += 1
            continue

        risk_per_share = plan.entry - plan.stop
        if risk_per_share <= 0:
            i += 1
            continue

        result_r, held, outcome = _simulate_trade(
            data,
            fill_index,
            plan.entry,
            plan.stop,
            plan.target,
            risk_per_share,
            spec.max_hold_days,
        )
        stats.signals += 1
        results.append(result_r)
        bars_held.append(held)
        if outcome == "win":
            stats.wins += 1
        elif outcome == "loss":
            stats.losses += 1
        else:
            stats.timeouts += 1

        # Не считаем перекрывающиеся сделки по одной бумаге.
        i = fill_index + held
        continue

    if stats.signals:
        stats.expectancy_r = round(float(np.mean(results)), 3)
        stats.avg_bars_held = round(float(np.mean(bars_held)), 1)
    return stats


def passes_history_filter(stats: BacktestStats, spec: TradeSpec) -> tuple[bool, str]:
    """Проверяет минимальные требования к исторической состоятельности сетапа."""
    if spec.min_backtest_signals <= 0:
        return True, ""
    if stats.signals < spec.min_backtest_signals:
        # Недостаточно истории — не отбрасываем, но и не считаем плюсом.
        return True, "мало исторических сигналов"
    if stats.hit_rate < spec.min_backtest_hit_rate:
        return False, f"история: только {stats.hit_rate:.0%} успешных сделок"
    if stats.expectancy_r < spec.min_backtest_expectancy_r:
        return False, f"история: отрицательное матожидание {stats.expectancy_r:+.2f}R"
    return True, ""


def breakeven_hit_rate(spec: TradeSpec) -> float:
    """Порог безубыточности по частоте успеха при заданном R:R."""
    rr = spec.min_rr
    if not math.isfinite(rr) or rr <= 0:
        return math.nan
    return 1.0 / (1.0 + rr)
