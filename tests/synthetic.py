"""Генераторы синтетических котировок для тестов.

Данные строятся детерминированно (фиксированный seed), чтобы тесты не зависели
ни от сети, ни от случайности.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BUSINESS_END = "2026-08-20"


def _frame(close: np.ndarray, high: np.ndarray, low: np.ndarray, volume: np.ndarray) -> pd.DataFrame:
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.bdate_range(end=BUSINESS_END, periods=len(close))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def breakout_stock(
    *,
    trend_bars: int = 260,
    base_bars: int = 14,
    start: float = 20.0,
    end: float = 60.0,
    trend_intrabar: float = 0.012,
    base_intrabar: float = 0.005,
    base_width: float = 0.02,
    noise: float = 0.004,
    seed: int = 7,
) -> pd.DataFrame:
    """Долгий аптренд, затем сжатая консолидация у максимума на низком объёме."""
    rng = np.random.default_rng(seed)
    trend = np.exp(
        np.linspace(np.log(start), np.log(end), trend_bars)
        + rng.normal(0, noise, trend_bars)
    )
    top = float(trend[-1])
    phase = np.linspace(0, 2 * np.pi * 1.5, base_bars)
    base = top * (1 - base_width * (0.5 + 0.5 * np.cos(phase)) * 0.5)
    close = np.concatenate([trend, base])
    spread = np.concatenate(
        [np.full(trend_bars, trend_intrabar), np.full(base_bars, base_intrabar)]
    )
    volume = np.concatenate(
        [
            rng.integers(900_000, 1_100_000, trend_bars),
            rng.integers(500_000, 600_000, base_bars),
        ]
    ).astype(float)
    return _frame(close, close * (1 + spread), close * (1 - spread), volume)


def downtrend_stock(bars: int = 300, start: float = 80.0, end: float = 30.0, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = np.exp(np.linspace(np.log(start), np.log(end), bars) + rng.normal(0, 0.006, bars))
    volume = rng.integers(800_000, 1_200_000, bars).astype(float)
    return _frame(close, close * 1.012, close * 0.988, volume)


def flat_stock(bars: int = 300, price: float = 25.0, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = price * (1 + rng.normal(0, 0.004, bars))
    volume = rng.integers(800_000, 1_200_000, bars).astype(float)
    return _frame(close, close * 1.01, close * 0.99, volume)


def penny_stock(bars: int = 300, seed: int = 5) -> pd.DataFrame:
    """Та же форма, что и у пробойной бумаги, но цена ниже $5."""
    frame = breakout_stock(seed=seed)
    scaled = frame[["Open", "High", "Low", "Close"]] * (2.5 / frame["Close"].iloc[-1])
    scaled["Volume"] = frame["Volume"]
    return scaled.iloc[-bars:]


def multi_ticker_frame(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Собирает выдачу в формате yf.download(group_by='ticker')."""
    pieces = {}
    for symbol, frame in frames.items():
        for column in ("Open", "High", "Low", "Close", "Volume"):
            pieces[(symbol, column)] = frame[column]
    combined = pd.DataFrame(pieces)
    combined.columns = pd.MultiIndex.from_tuples(combined.columns)
    return combined


def repeated_breakouts_stock(
    *,
    cycles: int = 18,
    rise_bars: int = 12,
    base_bars: int = 12,
    start: float = 15.0,
    cycle_gain: float = 0.09,
    base_width: float = 0.02,
    trend_intrabar: float = 0.012,
    base_intrabar: float = 0.005,
    seed: int = 42,
) -> pd.DataFrame:
    """Лестница «рост -> сжатая база -> пробой», повторённая много раз.

    Нужна для бэктеста: на такой истории правило входа срабатывает
    неоднократно, поэтому статистика сделок считается по-настоящему.
    """
    rng = np.random.default_rng(seed)
    closes: list[float] = []
    spreads: list[float] = []
    volumes: list[float] = []
    price = start
    for _ in range(cycles):
        rise = np.exp(
            np.linspace(np.log(price), np.log(price * (1 + cycle_gain)), rise_bars)
            + rng.normal(0, 0.003, rise_bars)
        )
        closes.extend(rise.tolist())
        spreads.extend([trend_intrabar] * rise_bars)
        volumes.extend(rng.integers(900_000, 1_100_000, rise_bars).tolist())

        top = float(rise[-1])
        phase = np.linspace(0, 2 * np.pi * 1.5, base_bars)
        base = top * (1 - base_width * (0.5 + 0.5 * np.cos(phase)) * 0.5)
        closes.extend(base.tolist())
        spreads.extend([base_intrabar] * base_bars)
        volumes.extend(rng.integers(500_000, 600_000, base_bars).tolist())
        price = float(base[-1])

    close = np.array(closes)
    spread = np.array(spreads)
    return _frame(close, close * (1 + spread), close * (1 - spread), np.array(volumes, dtype=float))
