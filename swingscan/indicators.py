"""Технические индикаторы на дневных барах.

Все функции работают с ``pandas.Series``/``DataFrame`` и возвращают серии той же
длины с ``NaN`` в периоде прогрева. Реализации сглаживания — по Уайлдеру
(RMA), как в классических определениях ATR/RSI/ADX.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def rma(series: pd.Series, period: int) -> pd.Series:
    """Сглаживание Уайлдера (RMA), эквивалент EMA с alpha = 1/period."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return rma(true_range(df), period)


def atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR в долях от цены закрытия (0.02 == 2%)."""
    return atr(df, period) / df["Close"]


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    result = 100.0 - (100.0 / (1.0 + rs))
    # Когда убытков не было вовсе, RSI = 100.
    return result.where(avg_loss.ne(0.0) | avg_gain.isna(), 100.0)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["High"], df["Low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )
    tr = rma(true_range(df), period).replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, period) / tr
    minus_di = 100.0 * rma(minus_dm, period) / tr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return rma(dx, period)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": macd_line - signal_line,
        }
    )


def donchian_high(series: pd.Series, period: int) -> pd.Series:
    """Максимум за ``period`` баров, не включая текущий бар."""
    return series.shift(1).rolling(period, min_periods=period).max()


def donchian_low(series: pd.Series, period: int) -> pd.Series:
    return series.shift(1).rolling(period, min_periods=period).min()


def rolling_range_pct(df: pd.DataFrame, period: int) -> pd.Series:
    """Ширина торгового диапазона за период в долях от минимума (сжатие базы)."""
    highest = df["High"].rolling(period, min_periods=period).max()
    lowest = df["Low"].rolling(period, min_periods=period).min()
    return (highest - lowest) / lowest.replace(0.0, np.nan)


def dollar_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return (df["Close"] * df["Volume"]).rolling(period, min_periods=1).mean()


def volume_ratio(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    """Отношение среднего объёма за fast дней к среднему за slow дней."""
    fast_avg = df["Volume"].rolling(fast, min_periods=fast).mean()
    slow_avg = df["Volume"].rolling(slow, min_periods=slow).mean()
    return fast_avg / slow_avg.replace(0.0, np.nan)


def pct_change_n(series: pd.Series, period: int) -> pd.Series:
    return series.pct_change(period)


def distance_to_high(series: pd.Series, period: int = 252) -> pd.Series:
    """Насколько цена ниже максимума за период (0.05 == на 5% ниже хая)."""
    highest = series.rolling(period, min_periods=min(period, 60)).max()
    return (highest - series) / highest.replace(0.0, np.nan)


def distance_from_low(series: pd.Series, period: int = 252) -> pd.Series:
    """Насколько цена выше минимума за период (0.5 == на 50% выше лоу)."""
    lowest = series.rolling(period, min_periods=min(period, 60)).min()
    return (series - lowest) / lowest.replace(0.0, np.nan)


def relative_strength(
    close: pd.Series, benchmark_close: pd.Series, period: int = 63
) -> pd.Series:
    """Относительная сила: доходность бумаги минус доходность бенчмарка."""
    aligned = benchmark_close.reindex(close.index).ffill()
    return close.pct_change(period) - aligned.pct_change(period)


def add_indicators(
    df: pd.DataFrame, benchmark_close: pd.Series | None = None
) -> pd.DataFrame:
    """Дополняет OHLCV-таблицу набором индикаторов, используемых стратегиями."""
    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"В данных отсутствуют колонки: {', '.join(missing)}")

    out = df.copy()
    close = out["Close"]

    out["sma20"] = sma(close, 20)
    out["sma50"] = sma(close, 50)
    out["sma150"] = sma(close, 150)
    out["sma200"] = sma(close, 200)
    out["ema10"] = ema(close, 10)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)

    out["atr14"] = atr(out, 14)
    out["atr50"] = atr(out, 50)
    out["atr_pct"] = out["atr14"] / close
    out["atr_squeeze"] = out["atr14"] / out["atr50"].replace(0.0, np.nan)

    out["rsi14"] = rsi(close, 14)
    out["adx14"] = adx(out, 14)
    out = out.join(macd(close))

    out["high20"] = donchian_high(out["High"], 20)
    out["high50"] = donchian_high(out["High"], 50)
    out["low10"] = donchian_low(out["Low"], 10)
    out["low20"] = donchian_low(out["Low"], 20)

    out["range10_pct"] = rolling_range_pct(out, 10)
    out["range20_pct"] = rolling_range_pct(out, 20)

    out["avg_volume20"] = out["Volume"].rolling(20, min_periods=20).mean()
    out["dollar_volume20"] = dollar_volume(out, 20)
    out["volume_ratio"] = volume_ratio(out)
    out["volume_spike"] = out["Volume"] / out["avg_volume20"].replace(0.0, np.nan)

    out["ret_5d"] = pct_change_n(close, 5)
    out["ret_20d"] = pct_change_n(close, 20)
    out["ret_63d"] = pct_change_n(close, 63)
    out["dist_52w_high"] = distance_to_high(close, 252)
    out["above_52w_low"] = distance_from_low(close, 252)

    if benchmark_close is not None and len(benchmark_close) > 0:
        out["rs_63d"] = relative_strength(close, benchmark_close, 63)
        out["rs_20d"] = relative_strength(close, benchmark_close, 20)
    else:
        out["rs_63d"] = np.nan
        out["rs_20d"] = np.nan

    return out
