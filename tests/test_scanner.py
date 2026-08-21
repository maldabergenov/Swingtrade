"""Сквозные тесты сканера на синтетических данных (без сети)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from swingscan.config import Settings, TradeSpec, UniverseFilters
from swingscan.market_data import MarketData, Quote
from swingscan.scanner import Scanner
from swingscan.universe import SymbolInfo
from tests.synthetic import (
    breakout_stock,
    downtrend_stock,
    flat_stock,
    multi_ticker_frame,
    penny_stock,
)

ALMATY = ZoneInfo("Asia/Almaty")
MORNING = datetime(2026, 8, 21, 9, 0, tzinfo=ALMATY)  # 04:00 UTC, рынок США закрыт
EVENING = datetime(2026, 8, 21, 21, 0, tzinfo=ALMATY)  # 16:00 UTC, идут торги


def build_frames() -> dict[str, pd.DataFrame]:
    illiquid = breakout_stock(seed=21)
    illiquid["Volume"] = 8_000.0

    return {
        "GOOD": breakout_stock(),
        "GOOD2": breakout_stock(seed=33, start=15.0, end=48.0),
        "CHEAP": penny_stock(),
        "SMALLCAP": breakout_stock(seed=13),
        "NOCAP": breakout_stock(seed=17),
        "ILLIQUID": illiquid,
        "DOWN": downtrend_stock(bars=300),
        "FLAT": flat_stock(bars=300),
        "SPY": flat_stock(bars=300, price=400.0),
    }


QUOTES = {
    "GOOD": Quote("GOOD", 60.0, 2.0e9),
    "GOOD2": Quote("GOOD2", 48.0, 8.0e8),
    "CHEAP": Quote("CHEAP", 2.5, 3.0e8),
    "SMALLCAP": Quote("SMALLCAP", 60.0, 1.0e7),  # ниже $50 млн
    "ILLIQUID": Quote("ILLIQUID", 60.0, 5.0e8),
    "DOWN": Quote("DOWN", 30.0, 4.0e9),
    "FLAT": Quote("FLAT", 25.0, 4.0e9),
    # NOCAP намеренно отсутствует — капитализацию подтвердить нечем
}


def make_scanner(settings: Settings | None = None, frames=None, quotes=None) -> Scanner:
    frames = frames or build_frames()
    quotes = QUOTES if quotes is None else quotes
    names = [
        SymbolInfo(symbol, f"{symbol} Corporation", "NASDAQ", False)
        for symbol in frames
        if symbol != "SPY"
    ]
    market_data = MarketData(
        use_cache=False,
        downloader=lambda batch: multi_ticker_frame(
            {s: frames[s] for s in batch if s in frames}
        ),
        quote_fetcher=lambda symbols: {s: quotes[s] for s in symbols if s in quotes},
    )
    base = settings or Settings()
    return Scanner(base, market_data=market_data, universe_provider=lambda: names)


@pytest.fixture()
def scan_settings(tmp_path) -> Settings:
    base = Settings()
    return base.with_overrides(
        state_dir=tmp_path / "state",
        reports_dir=tmp_path / "reports",
        data=type(base.data)(cache_dir=tmp_path / "cache", use_cache=False),
    )


def test_scan_returns_only_qualifying_symbols(scan_settings):
    result = make_scanner(scan_settings).run(MORNING)
    symbols = {idea.symbol for idea in result.ideas}
    assert "GOOD" in symbols
    for rejected in ("CHEAP", "SMALLCAP", "NOCAP", "ILLIQUID", "DOWN", "FLAT", "SPY"):
        assert rejected not in symbols


def test_every_idea_satisfies_the_specification(scan_settings):
    result = make_scanner(scan_settings).run(MORNING)
    assert result.ideas, "ожидалась хотя бы одна идея на синтетическом пробое"
    spec = scan_settings.trade
    for idea in result.ideas:
        assert idea.risk_pct <= spec.max_risk_pct + 1e-9
        assert idea.reward_pct >= spec.min_target_pct - 1e-9
        assert idea.rr >= spec.min_rr - 1e-6
        assert idea.stop < idea.entry < idea.target
        assert idea.last_close >= scan_settings.filters.min_price
        assert idea.market_cap >= scan_settings.filters.min_market_cap
        assert idea.min_hold_days == 10 and idea.max_hold_days == 15
        assert idea.deadline > idea.as_of
        assert 0.0 <= idea.score <= 100.0
        assert idea.reasons


def test_stop_and_target_arithmetic_matches_percentages(scan_settings):
    idea = make_scanner(scan_settings).run(MORNING).ideas[0]
    assert (idea.entry - idea.stop) / idea.entry == pytest.approx(idea.risk_pct, abs=1e-3)
    assert (idea.target - idea.entry) / idea.entry == pytest.approx(idea.reward_pct, abs=1e-3)


def test_funnel_counts_are_monotonic(scan_settings):
    funnel = make_scanner(scan_settings).run(MORNING).funnel
    assert funnel.universe == 8
    assert funnel.with_data >= funnel.passed_price >= funnel.passed_liquidity
    assert funnel.passed_liquidity >= funnel.passed_market_cap
    assert funnel.passed_setup >= funnel.passed_risk_reward >= funnel.passed_feasibility
    assert funnel.passed_feasibility >= funnel.passed_backtest


def test_price_filter_excludes_sub_five_dollar_stock(scan_settings):
    frames = {"CHEAP": penny_stock(), "SPY": flat_stock(bars=300, price=400.0)}
    result = make_scanner(scan_settings, frames=frames).run(MORNING)
    assert result.funnel.passed_price == 0
    assert result.ideas == []


def test_market_cap_filter_excludes_small_companies(scan_settings):
    frames = {"SMALLCAP": breakout_stock(seed=13), "SPY": flat_stock(bars=300, price=400.0)}
    result = make_scanner(scan_settings, frames=frames).run(MORNING)
    assert result.funnel.passed_liquidity == 1
    assert result.funnel.passed_market_cap == 0
    assert result.ideas == []


def test_symbol_without_market_cap_is_skipped(scan_settings):
    frames = {"NOCAP": breakout_stock(seed=17), "SPY": flat_stock(bars=300, price=400.0)}
    result = make_scanner(scan_settings, frames=frames, quotes={}).run(MORNING)
    assert result.funnel.passed_market_cap == 0
    assert result.ideas == []


def test_short_history_is_skipped(scan_settings):
    frames = {"GOOD": breakout_stock().tail(120), "SPY": flat_stock(bars=300, price=400.0)}
    result = make_scanner(scan_settings, frames=frames).run(MORNING)
    assert result.funnel.passed_price == 0
    assert result.ideas == []


def test_results_are_sorted_by_score(scan_settings):
    ideas = make_scanner(scan_settings).run(MORNING).ideas
    assert [i.score for i in ideas] == sorted((i.score for i in ideas), reverse=True)


def test_max_results_limits_output(scan_settings):
    limited = scan_settings.with_overrides(max_results=1)
    assert len(make_scanner(limited).run(MORNING).ideas) <= 1


def test_min_score_filters_weak_ideas(scan_settings):
    strict = scan_settings.with_overrides(min_score=99.9)
    assert make_scanner(strict).run(MORNING).ideas == []


def test_evening_scan_is_marked_as_intraday(scan_settings):
    result = make_scanner(scan_settings).run(EVENING)
    assert result.partial_session is True
    morning = make_scanner(scan_settings).run(MORNING)
    assert morning.partial_session is False


def test_partial_bar_is_trimmed_when_disabled():
    """Незакрытый бар текущего дня отбрасывается, если так настроено."""
    frame = breakout_stock()
    now = datetime.now(timezone.utc)
    ny_today = pd.Timestamp(now.astimezone(ZoneInfo("America/New_York")).date())
    frame.index = pd.DatetimeIndex(
        pd.bdate_range(end=ny_today, periods=len(frame))
    )
    trimmed = Scanner._trim_partial_bar(frame, now, keep_partial=False)
    assert len(trimmed) == len(frame) - 1
    assert Scanner._trim_partial_bar(frame, now, keep_partial=True) is frame


def test_universe_failure_is_reported_not_raised(scan_settings):
    def broken():
        raise RuntimeError("nasdaqtrader.com недоступен")

    scanner = Scanner(scan_settings, market_data=MarketData(use_cache=False, downloader=lambda b: pd.DataFrame()), universe_provider=broken)
    result = scanner.run(MORNING)
    assert result.ideas == []
    assert any("nasdaqtrader" in error for error in result.errors)


def test_quote_failure_is_reported_not_raised(scan_settings):
    frames = build_frames()

    def broken_quotes(_symbols):
        raise RuntimeError("Yahoo вернул 429")

    market_data = MarketData(
        use_cache=False,
        downloader=lambda batch: multi_ticker_frame({s: frames[s] for s in batch if s in frames}),
        quote_fetcher=broken_quotes,
    )
    names = [SymbolInfo(s, s, "NASDAQ", False) for s in frames if s != "SPY"]
    result = Scanner(scan_settings, market_data=market_data, universe_provider=lambda: names).run(MORNING)
    assert result.ideas == []
    assert any("429" in error for error in result.errors)


def test_symbol_limit_truncates_universe(scan_settings):
    limited = scan_settings.with_overrides(filters=UniverseFilters(max_symbols=2))
    assert make_scanner(limited).run(MORNING).funnel.universe == 2


def test_unreachable_target_yields_no_ideas(scan_settings):
    """Цель +50% за 3 недели нереализуема при такой волатильности — выдача пуста."""
    strict = scan_settings.with_overrides(trade=TradeSpec(min_target_pct=0.50))
    result = make_scanner(strict).run(MORNING)
    assert result.funnel.passed_setup > 0  # сетапы находятся
    assert result.funnel.passed_feasibility == 0  # но план нереализуем
    assert result.ideas == []


def test_thin_history_is_disclosed_in_reasons(scan_settings):
    """Если исторических сигналов мало, идея не отбрасывается, но помечается."""
    result = make_scanner(scan_settings).run(MORNING)
    idea = result.ideas[0]
    if idea.backtest.signals < scan_settings.trade.min_backtest_signals:
        assert any("мало исторических сигналов" in reason for reason in idea.reasons)


def test_disabled_history_filter_keeps_setups(scan_settings):
    permissive = scan_settings.with_overrides(trade=TradeSpec(min_backtest_signals=0))
    result = make_scanner(permissive).run(MORNING)
    assert result.funnel.passed_backtest == result.funnel.passed_feasibility


def test_result_serializes_to_json_dict(scan_settings):
    payload = make_scanner(scan_settings).run(MORNING).to_dict()
    assert payload["funnel"]["universe"] == 8
    assert isinstance(payload["ideas"], list)
    assert payload["duration_seconds"] >= 0
    if payload["ideas"]:
        idea = payload["ideas"][0]
        assert {"symbol", "entry", "stop", "target", "score", "backtest"} <= set(idea)
        assert "hit_rate" in idea["backtest"]


def test_total_data_outage_is_recorded_as_error(scan_settings):
    """Если не загрузилось ничего — это ошибка, а не «пусто по рынку»."""
    market_data = MarketData(use_cache=False, downloader=lambda _b: pd.DataFrame())
    names = [SymbolInfo("GOOD", "GOOD", "NASDAQ", False)]
    result = Scanner(scan_settings, market_data=market_data, universe_provider=lambda: names).run(MORNING)
    assert result.funnel.with_data == 0
    assert result.ideas == []
    assert any("котировки" in error for error in result.errors)


def test_stale_symbol_is_skipped(scan_settings):
    """Бумага, отставшая от рынка на неделю, в отбор не попадает."""
    frames = build_frames()
    stale = frames["GOOD"].copy()
    stale.index = stale.index - pd.Timedelta(days=30)
    frames["GOOD"] = stale

    result = make_scanner(scan_settings, frames=frames).run(MORNING)
    assert "GOOD" not in {idea.symbol for idea in result.ideas}
    assert "GOOD2" in {idea.symbol for idea in result.ideas}


def test_report_date_matches_idea_date(scan_settings):
    result = make_scanner(scan_settings).run(MORNING)
    assert result.as_of is not None
    for idea in result.ideas:
        assert idea.as_of == result.as_of
