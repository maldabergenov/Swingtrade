"""Логика отбора сетапов и построения торгового плана.

Ключевой принцип: одна и та же функция ``detect_setup`` используется и для
текущего бара (живой скан), и для исторических баров (бэктест). Поэтому
статистика в отчёте описывает ровно то правило, по которому выдан сигнал.

Про соотношение цель/риск. ТЗ требует потенциал >= +20% при риске <= 2%, то
есть R:R >= 10:1. Это возможно только в узкой полосе волатильности:

* стоп 2% не должен находиться внутри дневного шума  ->  ATR% <= 2% / 0.75
* цель +20% должна быть достижима за 10-15 сессий     ->  ATR% >= 20% / 15

Поэтому кандидат обязан пройти проверку ``check_feasibility``: она не даёт
выдавать «бумажные» сделки, которые формально проходят по цифрам, но в реальном
рынке гарантированно выбивают стоп или не доходят до цели.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import TradeSpec
from .models import BREAKOUT, MOMENTUM, PULLBACK


@dataclass(frozen=True)
class RawSignal:
    """Сырой сигнал до наложения ограничений по риску и цели."""

    setup_code: str
    trigger: float  # цена срабатывания (до буфера)
    structural_stop: float  # стоп по структуре графика
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TradePlan:
    entry: float
    stop: float
    target: float
    risk_pct: float
    reward_pct: float
    rr: float


@dataclass(frozen=True)
class FeasibilityResult:
    ok: bool
    reason: str = ""


_ARRAY_COLUMNS = (
    "Open",
    "High",
    "Low",
    "Close",
    "sma50",
    "sma150",
    "ema20",
    "ema50",
    "adx14",
    "rsi14",
    "atr_pct",
    "atr_squeeze",
    "high20",
    "range10_pct",
    "range20_pct",
    "volume_ratio",
    "ret_20d",
    "dist_52w_high",
    "above_52w_low",
    "rs_63d",
    "dollar_volume20",
    "avg_volume20",
)


@dataclass(frozen=True)
class PreparedFrame:
    """Numpy-представление таблицы с индикаторами.

    Детекторы вызываются по одному разу на бар (в бэктесте — сотни раз на
    бумагу), поэтому массивы извлекаются один раз, а не на каждом вызове.
    """

    index: pd.DatetimeIndex
    columns: dict[str, np.ndarray]

    def __len__(self) -> int:
        return len(self.index)

    def get(self, name: str, i: int) -> float:
        arr = self.columns.get(name)
        if arr is None or i < 0 or i >= arr.shape[0]:
            return math.nan
        return float(arr[i])

    def array(self, name: str) -> np.ndarray:
        return self.columns[name]


def prepare(df: pd.DataFrame) -> PreparedFrame:
    columns: dict[str, np.ndarray] = {}
    for name in _ARRAY_COLUMNS:
        if name in df.columns:
            columns[name] = df[name].to_numpy(dtype="float64", na_value=math.nan)
        else:
            columns[name] = np.full(len(df), math.nan)
    return PreparedFrame(pd.DatetimeIndex(df.index), columns)


# --- пороги детекторов (вынесены в константы, чтобы их было видно и тестировать)
MIN_ADX = 18.0
MAX_DIST_52W_HIGH = 0.25
MIN_ABOVE_52W_LOW = 0.30
BO_MAX_BASE_RANGE = 0.18
BO_MIN_CLOSE_VS_HIGH = 0.95
BO_MAX_VOLUME_RATIO = 1.15
MOM_MAX_DIST_HIGH = 0.06
MOM_MIN_RET_20D = 0.05
MOM_MAX_RANGE10 = 0.10
PB_MIN_DEPTH = 0.03
PB_MAX_DEPTH = 0.15
PB_MIN_RSI = 35.0
PB_MAX_RSI = 58.0


def _finite(*values: float) -> bool:
    return all(v is not None and not math.isnan(v) and not math.isinf(v) for v in values)


def trend_ok(data: PreparedFrame, i: int) -> tuple[bool, list[str]]:
    """Фильтр восходящего тренда — общий вход для всех лонговых сетапов."""
    reasons: list[str] = []
    close = data.get("Close", i)
    sma50 = data.get("sma50", i)
    sma150 = data.get("sma150", i)
    sma50_prev = data.get("sma50", i - 10) if i >= 10 else math.nan
    adx_value = data.get("adx14", i)
    dist_high = data.get("dist_52w_high", i)
    above_low = data.get("above_52w_low", i)

    if not _finite(close, sma50, sma150, adx_value, dist_high, above_low):
        return False, reasons
    if close <= sma50 or sma50 <= sma150:
        return False, reasons
    if not _finite(sma50_prev) or sma50 <= sma50_prev:
        return False, reasons
    if adx_value < MIN_ADX:
        return False, reasons
    if dist_high > MAX_DIST_52W_HIGH:
        return False, reasons
    if above_low < MIN_ABOVE_52W_LOW:
        return False, reasons

    reasons.append(f"тренд вверх: цена > SMA50 > SMA150, ADX {adx_value:.0f}")
    if dist_high <= 0.05:
        reasons.append(f"у 52-недельного максимума (−{dist_high * 100:.1f}%)")
    return True, reasons


def detect_setup(data: PreparedFrame, i: int) -> RawSignal | None:
    """Возвращает сигнал для бара ``i`` либо ``None``.

    Порядок проверки: пробой сжатой базы, импульс у хаёв, откат к скользящей.
    Функция используется и живым сканером, и бэктестом — правило одно и то же.
    """
    if i < 60 or i >= len(data):
        return None

    ok, trend_reasons = trend_ok(data, i)
    if not ok:
        return None

    high = data.array("High")
    low = data.array("Low")

    close = data.get("Close", i)
    open_price = data.get("Open", i)
    high20 = data.get("high20", i)
    range20 = data.get("range20_pct", i)
    range10 = data.get("range10_pct", i)
    squeeze = data.get("atr_squeeze", i)
    vol_ratio = data.get("volume_ratio", i)
    rsi_value = data.get("rsi14", i)
    ret20 = data.get("ret_20d", i)
    dist_high = data.get("dist_52w_high", i)
    ema20_value = data.get("ema20", i)
    ema50_value = data.get("ema50", i)

    if not _finite(close, high20) or high20 <= 0:
        return None

    # --- 1. Пробой сжатой консолидации -------------------------------------
    if (
        _finite(range20, squeeze, vol_ratio)
        and range20 <= BO_MAX_BASE_RANGE
        and squeeze <= 1.0
        and close >= BO_MIN_CLOSE_VS_HIGH * high20
        and vol_ratio <= BO_MAX_VOLUME_RATIO
    ):
        trigger = max(high20, float(high[i]))
        structural_stop = float(np.min(low[max(0, i - 9) : i + 1]))
        reasons = list(trend_reasons)
        reasons.append(f"база сжата: диапазон 20 дней {range20 * 100:.1f}%")
        reasons.append(f"сжатие волатильности: ATR14/ATR50 = {squeeze:.2f}")
        reasons.append(f"объём подсох: 5д/20д = {vol_ratio:.2f}")
        return RawSignal(BREAKOUT.code, trigger, structural_stop, tuple(reasons))

    # --- 2. Импульс у годовых максимумов -----------------------------------
    if (
        _finite(dist_high, ret20, range10)
        and dist_high <= MOM_MAX_DIST_HIGH
        and ret20 >= MOM_MIN_RET_20D
        and range10 <= MOM_MAX_RANGE10
    ):
        trigger = max(high20, float(high[i]))
        structural_stop = float(np.min(low[max(0, i - 4) : i + 1]))
        reasons = list(trend_reasons)
        reasons.append(f"импульс: +{ret20 * 100:.1f}% за 20 сессий")
        reasons.append(f"тугая полка у хая: диапазон 10 дней {range10 * 100:.1f}%")
        return RawSignal(MOMENTUM.code, trigger, structural_stop, tuple(reasons))

    # --- 3. Откат к скользящей в тренде ------------------------------------
    if _finite(rsi_value, ema20_value, ema50_value, open_price):
        depth = (high20 - close) / high20
        bullish_bar = close > open_price or close > (high[i] + low[i]) / 2.0
        if (
            PB_MIN_DEPTH <= depth <= PB_MAX_DEPTH
            and PB_MIN_RSI <= rsi_value <= PB_MAX_RSI
            and close >= ema50_value * 0.98
            and close <= ema20_value * 1.03
            and bullish_bar
        ):
            trigger = float(high[i])
            structural_stop = float(np.min(low[max(0, i - 2) : i + 1]))
            reasons = list(trend_reasons)
            reasons.append(f"откат {depth * 100:.1f}% к EMA20 при RSI {rsi_value:.0f}")
            reasons.append("бар разворота: закрытие в верхней половине диапазона")
            return RawSignal(PULLBACK.code, trigger, structural_stop, tuple(reasons))

    return None


def build_plan(signal: RawSignal, spec: TradeSpec) -> TradePlan:
    """Строит вход/стоп/цель с жёстким ограничением риска сверху.

    Стоп берётся по структуре графика, но не дальше ``spec.max_risk_pct`` от
    входа — риск на сделку не может превысить лимит ТЗ. Если структура даёт
    более тугой стоп, используется он (риск получается меньше лимита).
    """
    entry = signal.trigger * (1.0 + spec.entry_buffer_pct)
    risk_floor = entry * (1.0 - spec.max_risk_pct)
    stop = max(signal.structural_stop, risk_floor)
    target = entry * (1.0 + spec.min_target_pct)

    risk_pct = (entry - stop) / entry if entry > 0 else math.nan
    reward_pct = (target - entry) / entry if entry > 0 else math.nan
    rr = reward_pct / risk_pct if risk_pct > 0 else math.inf
    return TradePlan(entry, stop, target, risk_pct, reward_pct, rr)


def check_risk_reward(plan: TradePlan, spec: TradeSpec) -> FeasibilityResult:
    """Прямая проверка требований ТЗ."""
    if not _finite(plan.risk_pct, plan.reward_pct):
        return FeasibilityResult(False, "некорректный расчёт риска")
    if plan.risk_pct <= 0:
        return FeasibilityResult(False, "стоп не ниже входа")
    if plan.risk_pct > spec.max_risk_pct + 1e-9:
        return FeasibilityResult(False, f"риск {plan.risk_pct:.2%} > лимита")
    if plan.reward_pct < spec.min_target_pct - 1e-9:
        return FeasibilityResult(False, f"потенциал {plan.reward_pct:.2%} < 20%")
    if plan.rr < spec.min_rr - 1e-6:
        return FeasibilityResult(False, f"R:R {plan.rr:.1f} < {spec.min_rr:.0f}")
    return FeasibilityResult(True)


def check_feasibility(
    plan: TradePlan, atr_pct_value: float, recent_low: float, spec: TradeSpec
) -> FeasibilityResult:
    """Проверяет, что план реализуем при текущей волатильности бумаги."""
    if not _finite(atr_pct_value) or atr_pct_value <= 0:
        return FeasibilityResult(False, "нет данных по ATR")

    # 1. Стоп не внутри дневного шума.
    if plan.risk_pct < spec.min_stop_atr_ratio * atr_pct_value:
        return FeasibilityResult(
            False,
            f"стоп {plan.risk_pct:.2%} внутри шума (ATR {atr_pct_value:.2%})",
        )

    # 2. Цель достижима за горизонт удержания.
    reachable = atr_pct_value * spec.max_hold_days * spec.max_target_atr_ratio
    if plan.reward_pct > reachable:
        return FeasibilityResult(
            False,
            f"цель {plan.reward_pct:.0%} недостижима за {spec.max_hold_days} сессий "
            f"(потенциал хода {reachable:.0%})",
        )

    # 3. Стоп должен стоять ниже последних минимумов, иначе вынос сразу.
    if _finite(recent_low) and plan.stop >= recent_low:
        return FeasibilityResult(False, "стоп выше локального минимума")

    return FeasibilityResult(True)


def _clip01(value: float) -> float:
    if not _finite(value):
        return 0.0
    return float(min(1.0, max(0.0, value)))


def volatility_fit(atr_pct_value: float, spec: TradeSpec) -> float:
    """Насколько ATR попадает в «рабочую полосу» для связки 20% / 2%.

    1.0 — центр полосы, 0.0 — за её границами.
    """
    low = spec.min_target_pct / (spec.max_hold_days * spec.max_target_atr_ratio)
    high = spec.max_risk_pct / spec.min_stop_atr_ratio
    if not _finite(atr_pct_value) or high <= low:
        return 0.0
    if atr_pct_value < low or atr_pct_value > high:
        return 0.0
    center = (low + high) / 2.0
    half = (high - low) / 2.0
    return _clip01(1.0 - abs(atr_pct_value - center) / half)


SCORE_WEIGHTS: dict[str, float] = {
    "trend": 0.16,
    "relative_strength": 0.16,
    "tightness": 0.12,
    "volatility_fit": 0.16,
    "proximity": 0.10,
    "liquidity": 0.08,
    "risk_reward": 0.10,
    "history": 0.12,
}


def score_idea(
    *,
    adx_value: float,
    rs_63d: float,
    range20: float,
    atr_pct_value: float,
    dist_high: float,
    avg_dollar_volume: float,
    rr: float,
    hit_rate: float,
    expectancy_r: float,
    spec: TradeSpec,
) -> tuple[float, dict[str, float]]:
    """Композитная оценка 0..100 и вклад каждой компоненты."""
    parts = {
        "trend": _clip01((adx_value - MIN_ADX) / (40.0 - MIN_ADX)),
        "relative_strength": _clip01((rs_63d + 0.05) / 0.35),
        "tightness": _clip01(1.0 - range20 / 0.20),
        "volatility_fit": volatility_fit(atr_pct_value, spec),
        "proximity": _clip01(1.0 - dist_high / MAX_DIST_52W_HIGH),
        "liquidity": _clip01(
            math.log10(max(avg_dollar_volume, 1.0) / 1e6) / 2.0
        ),  # $1M -> 0, $100M -> 1
        "risk_reward": _clip01((rr - spec.min_rr) / spec.min_rr),
        "history": _clip01(0.5 * (hit_rate / 0.35) + 0.5 * (expectancy_r / 2.0)),
    }
    total = sum(SCORE_WEIGHTS[k] * v for k, v in parts.items()) * 100.0
    return round(total, 1), {k: round(v, 3) for k, v in parts.items()}
