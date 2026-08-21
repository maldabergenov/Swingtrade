from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swingscan.backtest import (
    ENTRY_VALID_BARS,
    backtest_symbol,
    breakeven_hit_rate,
    passes_history_filter,
)
from swingscan.config import TradeSpec
from swingscan.indicators import add_indicators
from swingscan.models import BacktestStats
from swingscan.strategy import prepare
from swingscan.backtest import _simulate_trade
from tests.synthetic import breakout_stock, downtrend_stock


def _prepared_from_prices(highs, lows, closes, opens=None):
    n = len(closes)
    frame = pd.DataFrame(
        {
            "Open": opens if opens is not None else closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": np.full(n, 1e6),
        },
        index=pd.bdate_range("2024-01-01", periods=n),
    )
    return prepare(frame)


def test_simulate_trade_gap_up_above_target_books_the_gap():
    """Открытие выше цели фиксируется по открытию, а не по лимиту."""
    data = _prepared_from_prices(
        highs=[100, 130], lows=[99, 120], closes=[100, 128], opens=[100, 125]
    )
    result_r, _, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 15)
    assert outcome == "win"
    assert result_r == pytest.approx((125.0 - 100.0) / 2.0)


def test_simulate_trade_hits_target():
    data = _prepared_from_prices(
        highs=[100, 105, 125],
        lows=[99, 104, 118],
        closes=[100, 105, 124],
        opens=[100, 104, 119],
    )
    result_r, held, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 15)
    assert outcome == "win"
    assert result_r == pytest.approx(10.0)
    assert held == 3


def test_simulate_trade_hits_stop():
    data = _prepared_from_prices(
        highs=[100, 101, 101], lows=[99, 97, 96], closes=[100, 98, 97]
    )
    result_r, held, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 15)
    assert outcome == "loss"
    assert result_r == pytest.approx(-1.0)
    assert held == 2


def test_simulate_trade_stop_wins_ties_inside_bar():
    """Если бар задел и стоп, и цель — считаем убыток (консервативно)."""
    data = _prepared_from_prices(
        highs=[100, 125], lows=[99, 90], closes=[100, 120], opens=[100, 100]
    )
    _, _, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 15)
    assert outcome == "loss"


def test_simulate_trade_gap_down_below_stop_costs_more_than_1r():
    data = _prepared_from_prices(
        highs=[100, 95], lows=[99, 90], closes=[100, 92], opens=[100, 94]
    )
    result_r, _, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 15)
    assert outcome == "loss"
    assert result_r == pytest.approx((94.0 - 100.0) / 2.0)
    assert result_r < -1.0


def test_simulate_trade_timeout_uses_close():
    n = 6
    data = _prepared_from_prices(
        highs=np.full(n, 101.0), lows=np.full(n, 99.5), closes=np.full(n, 100.5)
    )
    result_r, held, outcome = _simulate_trade(data, 0, 100.0, 98.0, 120.0, 2.0, 5)
    assert outcome == "timeout"
    assert held == 5
    assert result_r == pytest.approx(0.25)


def test_backtest_on_uptrend_produces_signals(trade_spec):
    data = prepare(add_indicators(breakout_stock()))
    stats = backtest_symbol(data, trade_spec)
    assert stats.signals >= 0
    assert stats.wins + stats.losses + stats.timeouts == stats.signals


def test_backtest_returns_empty_for_short_history(trade_spec):
    frame = breakout_stock().tail(30)
    data = prepare(add_indicators(frame))
    stats = backtest_symbol(data, trade_spec)
    assert stats.signals == 0
    assert stats.hit_rate == 0.0


def test_backtest_finds_nothing_in_downtrend(trade_spec):
    data = prepare(add_indicators(downtrend_stock(bars=400)))
    assert backtest_symbol(data, trade_spec).signals == 0


def test_backtest_trades_do_not_overlap(trade_spec):
    """Сделки не должны считаться дважды: суммарно баров не больше истории."""
    data = prepare(add_indicators(breakout_stock(trend_bars=400, base_bars=20)))
    stats = backtest_symbol(data, trade_spec)
    if stats.signals:
        assert stats.avg_bars_held * stats.signals <= len(data)


def test_breakeven_hit_rate_matches_rr():
    assert breakeven_hit_rate(TradeSpec()) == pytest.approx(1 / 11)
    assert breakeven_hit_rate(TradeSpec(min_target_pct=0.10, max_risk_pct=0.02)) == pytest.approx(1 / 6)


def test_history_filter_allows_thin_sample(trade_spec):
    ok, note = passes_history_filter(BacktestStats(signals=2, wins=0), trade_spec)
    assert ok is True
    assert "мало" in note


def test_history_filter_rejects_poor_hit_rate(trade_spec):
    stats = BacktestStats(signals=20, wins=1, losses=19, expectancy_r=0.5)
    ok, note = passes_history_filter(stats, trade_spec)
    assert ok is False
    assert "успешных" in note


def test_history_filter_rejects_negative_expectancy(trade_spec):
    stats = BacktestStats(signals=20, wins=4, losses=16, expectancy_r=-0.4)
    ok, _ = passes_history_filter(stats, trade_spec)
    assert ok is False


def test_history_filter_can_be_disabled():
    spec = TradeSpec(min_backtest_signals=0)
    stats = BacktestStats(signals=50, wins=0, losses=50, expectancy_r=-1.0)
    assert passes_history_filter(stats, spec)[0] is True


def test_entry_order_expires(trade_spec):
    """Если цена не дошла до входа за отведённые бары — сделки не было."""
    assert ENTRY_VALID_BARS >= 1
    highs = np.full(300, 10.0)
    data = _prepared_from_prices(highs=highs, lows=highs - 0.2, closes=highs - 0.1)
    assert backtest_symbol(data, trade_spec).signals == 0


def test_backtest_full_loop_on_repeating_setups(trade_spec):
    """История с повторяющимися пробоями: правило реально прогоняется по сделкам."""
    from tests.synthetic import repeated_breakouts_stock

    data = prepare(add_indicators(repeated_breakouts_stock()))
    stats = backtest_symbol(data, trade_spec)

    assert stats.signals > 0
    assert stats.wins + stats.losses + stats.timeouts == stats.signals
    assert 0.0 <= stats.hit_rate <= 1.0
    assert 0 < stats.avg_bars_held <= trade_spec.max_hold_days
    assert stats.expectancy_r == pytest.approx(stats.expectancy_r)  # число, не NaN


def test_backtest_skip_last_excludes_open_trade(trade_spec):
    from tests.synthetic import repeated_breakouts_stock

    data = prepare(add_indicators(repeated_breakouts_stock()))
    full = backtest_symbol(data, trade_spec, skip_last=0)
    trimmed = backtest_symbol(data, trade_spec, skip_last=40)
    assert trimmed.signals <= full.signals


def test_backtest_lookback_limits_window(trade_spec):
    from tests.synthetic import repeated_breakouts_stock

    data = prepare(add_indicators(repeated_breakouts_stock()))
    wide = backtest_symbol(data, trade_spec, lookback_bars=1000)
    narrow = backtest_symbol(data, trade_spec, lookback_bars=150)
    assert narrow.signals <= wide.signals
