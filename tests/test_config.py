from __future__ import annotations

import pytest

from swingscan.config import Settings, TradeSpec, UniverseFilters, parse_hhmm


def test_defaults_match_specification():
    settings = Settings()
    assert settings.filters.min_price == 5.0
    assert settings.filters.min_market_cap == 50_000_000.0
    assert settings.trade.min_target_pct == 0.20
    assert settings.trade.max_risk_pct == 0.02
    assert settings.trade.min_hold_days == 10
    assert settings.trade.max_hold_days == 15
    assert settings.timezone == "Asia/Almaty"
    assert settings.scan_times == ("09:00", "21:00")


def test_min_rr_is_derived_from_target_and_risk():
    assert TradeSpec().min_rr == pytest.approx(10.0)
    assert TradeSpec(min_target_pct=0.15, max_risk_pct=0.03).min_rr == pytest.approx(5.0)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("MIN_PRICE", "7.5")
    monkeypatch.setenv("MIN_MARKET_CAP", "250_000_000")
    monkeypatch.setenv("MAX_RISK_PCT", "0.015")
    monkeypatch.setenv("SCAN_TIMES", "08:30, 20:15")
    monkeypatch.setenv("TIMEZONE", "Asia/Almaty")
    monkeypatch.setenv("EXCLUDE_ETF", "нет")

    settings = Settings.load(env_file=None)
    assert settings.filters.min_price == 7.5
    assert settings.filters.min_market_cap == 250_000_000
    assert settings.trade.max_risk_pct == 0.015
    assert settings.scan_times == ("08:30", "20:15")
    assert settings.filters.exclude_etf is False


def test_bad_number_raises(monkeypatch):
    monkeypatch.setenv("MIN_PRICE", "дорого")
    with pytest.raises(ValueError):
        UniverseFilters.from_env()


@pytest.mark.parametrize(
    "value,expected", [("09:00", (9, 0)), ("21:00", (21, 0)), ("00:30", (0, 30))]
)
def test_parse_hhmm(value, expected):
    assert parse_hhmm(value) == expected


@pytest.mark.parametrize("value", ["9", "25:00", "09:61", "утро"])
def test_parse_hhmm_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_hhmm(value)
