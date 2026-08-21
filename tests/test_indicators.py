from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swingscan.indicators import (
    add_indicators,
    adx,
    atr,
    distance_to_high,
    donchian_high,
    ema,
    relative_strength,
    rolling_range_pct,
    rsi,
    sma,
    volume_ratio,
)
from tests.synthetic import breakout_stock, flat_stock


def _series(values):
    return pd.Series(values, index=pd.bdate_range("2024-01-01", periods=len(values)), dtype="float64")


def test_sma_matches_manual_average():
    s = _series([1, 2, 3, 4, 5])
    result = sma(s, 3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_ema_first_value_is_simple_seed():
    s = _series([10, 11, 12, 13])
    result = ema(s, 2)
    # alpha = 2/(2+1); первое значение с min_periods=2
    assert result.iloc[1] == pytest.approx(10 + (11 - 10) * (2 / 3))


def test_atr_on_constant_range_equals_range():
    n = 40
    close = np.full(n, 100.0)
    df = pd.DataFrame(
        {
            "Open": close,
            "High": close + 2.0,
            "Low": close - 2.0,
            "Close": close,
            "Volume": np.full(n, 1e6),
        },
        index=pd.bdate_range("2024-01-01", periods=n),
    )
    assert atr(df, 14).iloc[-1] == pytest.approx(4.0)


def test_rsi_is_100_when_only_gains():
    s = _series(np.arange(1, 40, dtype="float64"))
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_zero_when_only_losses():
    s = _series(np.arange(40, 1, -1, dtype="float64"))
    assert rsi(s, 14).iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_rsi_stays_in_range_on_noisy_series():
    frame = flat_stock()
    values = rsi(frame["Close"], 14).dropna()
    assert values.between(0, 100).all()


def test_adx_high_in_strong_trend_and_low_when_flat():
    trending = adx(breakout_stock(base_bars=1), 14).iloc[-1]
    choppy = adx(flat_stock(), 14).iloc[-1]
    assert trending > choppy
    assert choppy < 30


def test_donchian_high_excludes_current_bar():
    s = _series([1, 5, 3, 9, 2])
    result = donchian_high(s, 2)
    assert result.iloc[2] == pytest.approx(5.0)  # max(1, 5)
    assert result.iloc[4] == pytest.approx(9.0)  # max(3, 9)


def test_rolling_range_pct():
    n = 5
    df = pd.DataFrame(
        {
            "Open": [10] * n,
            "High": [10, 11, 12, 11, 10],
            "Low": [9, 9, 10, 10, 10],
            "Close": [10] * n,
            "Volume": [1] * n,
        },
        index=pd.bdate_range("2024-01-01", periods=n),
    )
    assert rolling_range_pct(df, 3).iloc[2] == pytest.approx((12 - 9) / 9)


def test_volume_ratio_detects_dry_up():
    n = 40
    volume = np.concatenate([np.full(n - 5, 1_000_000.0), np.full(5, 400_000.0)])
    df = pd.DataFrame(
        {
            "Open": np.full(n, 10.0),
            "High": np.full(n, 10.1),
            "Low": np.full(n, 9.9),
            "Close": np.full(n, 10.0),
            "Volume": volume,
        },
        index=pd.bdate_range("2024-01-01", periods=n),
    )
    assert volume_ratio(df, 5, 20).iloc[-1] < 0.7


def test_distance_to_high_is_zero_at_new_high():
    rising = _series(np.linspace(10.0, 40.0, 120))
    assert distance_to_high(rising, 252).iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_distance_to_high_measures_drawdown():
    values = np.concatenate([np.linspace(10.0, 40.0, 100), np.full(20, 30.0)])
    assert distance_to_high(_series(values), 252).iloc[-1] == pytest.approx(0.25)


def test_relative_strength_against_benchmark():
    stock = _series(np.linspace(100, 150, 80))
    bench = _series(np.linspace(100, 110, 80))
    assert relative_strength(stock, bench, 63).iloc[-1] > 0


def test_add_indicators_provides_all_columns_used_by_strategy():
    frame = breakout_stock()
    enriched = add_indicators(frame)
    for column in (
        "sma50", "sma150", "ema20", "ema50", "atr14", "atr_pct", "atr_squeeze",
        "rsi14", "adx14", "high20", "range10_pct", "range20_pct", "volume_ratio",
        "ret_20d", "dist_52w_high", "above_52w_low", "dollar_volume20",
    ):
        assert column in enriched.columns, column
    assert len(enriched) == len(frame)


def test_add_indicators_rejects_missing_columns():
    with pytest.raises(ValueError, match="отсутствуют"):
        add_indicators(pd.DataFrame({"Close": [1.0, 2.0]}))
