from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swingscan.config import TradeSpec
from swingscan.indicators import add_indicators
from swingscan.strategy import (
    RawSignal,
    build_plan,
    check_feasibility,
    check_risk_reward,
    detect_setup,
    prepare,
    score_idea,
    trend_ok,
    volatility_fit,
)
from tests.synthetic import breakout_stock, downtrend_stock, flat_stock


def _prepared(frame: pd.DataFrame):
    return prepare(add_indicators(frame))


def test_trend_gate_accepts_uptrend_and_rejects_downtrend():
    up = _prepared(breakout_stock())
    down = _prepared(downtrend_stock())
    assert trend_ok(up, len(up) - 1)[0] is True
    assert trend_ok(down, len(down) - 1)[0] is False


def test_trend_gate_rejects_flat_market():
    flat = _prepared(flat_stock())
    assert trend_ok(flat, len(flat) - 1)[0] is False


def test_detect_setup_finds_breakout():
    data = _prepared(breakout_stock())
    signal = detect_setup(data, len(data) - 1)
    assert signal is not None
    assert signal.setup_code == "BO"
    assert signal.trigger > 0
    assert signal.structural_stop < signal.trigger
    assert any("база сжата" in reason for reason in signal.reasons)


def test_detect_setup_returns_none_without_history():
    data = _prepared(breakout_stock())
    assert detect_setup(data, 10) is None
    assert detect_setup(data, len(data) + 5) is None


def test_detect_setup_none_in_downtrend():
    data = _prepared(downtrend_stock())
    assert detect_setup(data, len(data) - 1) is None


def test_plan_caps_risk_at_limit(trade_spec):
    signal = RawSignal("BO", trigger=100.0, structural_stop=80.0, reasons=())
    plan = build_plan(signal, trade_spec)
    assert plan.risk_pct == pytest.approx(trade_spec.max_risk_pct)
    assert plan.stop == pytest.approx(plan.entry * 0.98)


def test_plan_keeps_tighter_structural_stop(trade_spec):
    signal = RawSignal("PB", trigger=100.0, structural_stop=99.3, reasons=())
    plan = build_plan(signal, trade_spec)
    assert plan.stop == pytest.approx(99.3)
    assert plan.risk_pct < trade_spec.max_risk_pct
    assert plan.rr > trade_spec.min_rr


def test_plan_target_is_exactly_min_target(trade_spec):
    plan = build_plan(RawSignal("BO", 50.0, 49.0, ()), trade_spec)
    assert plan.reward_pct == pytest.approx(trade_spec.min_target_pct)
    assert plan.target == pytest.approx(plan.entry * 1.20)


def test_entry_includes_buffer_above_trigger(trade_spec):
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), trade_spec)
    assert plan.entry > 100.0
    assert plan.entry == pytest.approx(100.0 * (1 + trade_spec.entry_buffer_pct))


def test_risk_reward_check_enforces_specification(trade_spec):
    good = build_plan(RawSignal("BO", 100.0, 98.5, ()), trade_spec)
    assert check_risk_reward(good, trade_spec).ok

    loose = TradeSpec(min_target_pct=0.20, max_risk_pct=0.02)
    bad = build_plan(RawSignal("BO", 100.0, 101.0, ()), loose)  # стоп выше входа
    assert not check_risk_reward(bad, loose).ok


def test_risk_reward_rejects_when_target_below_minimum():
    spec = TradeSpec(min_target_pct=0.20, max_risk_pct=0.02)
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), spec)
    strict = TradeSpec(min_target_pct=0.30, max_risk_pct=0.02)
    assert not check_risk_reward(plan, strict).ok


def test_feasibility_rejects_stop_inside_daily_noise(trade_spec):
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), trade_spec)
    result = check_feasibility(plan, atr_pct_value=0.06, recent_low=99.0, spec=trade_spec)
    assert not result.ok
    assert "шум" in result.reason


def test_feasibility_rejects_unreachable_target(trade_spec):
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), trade_spec)
    result = check_feasibility(plan, atr_pct_value=0.012, recent_low=99.0, spec=trade_spec)
    assert not result.ok
    assert "недостижима" in result.reason


def test_feasibility_rejects_stop_above_recent_low(trade_spec):
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), trade_spec)
    result = check_feasibility(plan, atr_pct_value=0.02, recent_low=98.0, spec=trade_spec)
    assert not result.ok
    assert "минимума" in result.reason


def test_feasibility_accepts_workable_volatility(trade_spec):
    plan = build_plan(RawSignal("BO", 100.0, 98.0, ()), trade_spec)
    assert check_feasibility(plan, 0.02, recent_low=99.0, spec=trade_spec).ok


def test_volatility_fit_band(trade_spec):
    low_edge = trade_spec.min_target_pct / trade_spec.max_hold_days
    high_edge = trade_spec.max_risk_pct / trade_spec.min_stop_atr_ratio
    assert volatility_fit(low_edge - 0.001, trade_spec) == 0.0
    assert volatility_fit(high_edge + 0.001, trade_spec) == 0.0
    assert volatility_fit((low_edge + high_edge) / 2, trade_spec) == pytest.approx(1.0)


def test_score_is_bounded_and_ordered(trade_spec):
    weak, _ = score_idea(
        adx_value=18.0, rs_63d=-0.05, range20=0.20, atr_pct_value=0.05,
        dist_high=0.25, avg_dollar_volume=1e6, rr=10.0, hit_rate=0.0,
        expectancy_r=0.0, spec=trade_spec,
    )
    strong, parts = score_idea(
        adx_value=40.0, rs_63d=0.30, range20=0.0, atr_pct_value=0.02,
        dist_high=0.0, avg_dollar_volume=2e8, rr=20.0, hit_rate=0.35,
        expectancy_r=2.0, spec=trade_spec,
    )
    assert 0.0 <= weak <= 100.0
    assert strong == pytest.approx(100.0)
    assert strong > weak
    assert set(parts) == {
        "trend", "relative_strength", "tightness", "volatility_fit",
        "proximity", "liquidity", "risk_reward", "history",
    }


def test_score_handles_nan_inputs(trade_spec):
    total, parts = score_idea(
        adx_value=float("nan"), rs_63d=float("nan"), range20=float("nan"),
        atr_pct_value=float("nan"), dist_high=float("nan"),
        avg_dollar_volume=float("nan"), rr=float("nan"), hit_rate=0.0,
        expectancy_r=0.0, spec=trade_spec,
    )
    assert total == 0.0
    assert all(value == 0.0 for value in parts.values())


def test_prepare_handles_missing_optional_columns():
    frame = breakout_stock()
    data = prepare(add_indicators(frame))
    assert np.isnan(data.get("rs_63d", len(data) - 1))  # без бенчмарка
    assert data.get("Close", len(data) - 1) > 0
