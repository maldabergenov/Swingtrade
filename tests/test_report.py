from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from swingscan.config import Settings
from swingscan.models import BacktestStats, FunnelStats, ScanResult, TradeIdea
from swingscan.report import (
    TELEGRAM_LIMIT,
    _money,
    format_console,
    format_idea,
    format_report,
    save_report,
    split_message,
)


def make_idea(**overrides) -> TradeIdea:
    base = dict(
        symbol="AAPL",
        name="Apple Inc.",
        setup_code="BO",
        as_of=date(2026, 8, 20),
        last_close=190.0,
        entry=192.4,
        stop=188.55,
        target=230.88,
        risk_pct=0.02,
        reward_pct=0.20,
        rr=10.0,
        atr_pct=0.021,
        adx=28.0,
        rsi=62.0,
        rs_63d=0.18,
        dist_52w_high=0.01,
        volume_ratio=0.8,
        avg_dollar_volume=1.45e8,
        market_cap=2.9e12,
        min_hold_days=10,
        max_hold_days=15,
        deadline=date(2026, 9, 11),
        score=87.4,
        reasons=["тренд вверх", "база сжата"],
        backtest=BacktestStats(signals=24, wins=5, losses=17, timeouts=2, expectancy_r=1.31),
    )
    base.update(overrides)
    return TradeIdea(**base)


def make_result(ideas=None, **overrides) -> ScanResult:
    started = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)
    base = dict(
        started_at=started,
        finished_at=started + timedelta(seconds=95),
        as_of=date(2026, 8, 20),
        ideas=ideas if ideas is not None else [make_idea()],
        funnel=FunnelStats(universe=4200, with_data=4100, passed_price=2600, passed_liquidity=1400, passed_market_cap=1300, passed_setup=64, passed_risk_reward=12, passed_feasibility=5, passed_backtest=4),
    )
    base.update(overrides)
    return ScanResult(**base)


@pytest.mark.parametrize(
    "value,expected",
    [(2.9e12, "$2.90T"), (3.4e9, "$3.40B"), (5.6e6, "$6M"), (4600, "$5K"), (120, "$120")],
)
def test_money_formatting(value, expected):
    assert _money(value) == expected


def test_format_idea_contains_key_numbers():
    text = format_idea(make_idea(), 1)
    assert "AAPL" in text
    assert "$192.40" in text
    assert "$188.55" in text
    assert "$230.88" in text
    assert "−2.00%" in text
    assert "+20.0%" in text
    assert "10.0:1" in text
    assert "24 сигн." in text


def test_format_idea_escapes_html_in_company_name():
    text = format_idea(make_idea(name="AT&T <Inc>"), 1)
    assert "&amp;" in text
    assert "<Inc>" not in text


def test_report_states_criteria_and_horizon():
    settings = Settings()
    text = format_report(make_result(), settings)
    assert "≥ $5" in text
    assert "$50M" in text
    assert "+20%" in text
    assert "≤ 2%" in text
    assert "10–15 сессий" in text


def test_report_marks_intraday_data():
    settings = Settings()
    text = format_report(make_result(partial_session=True), settings)
    assert "формируется" in text


def test_report_marks_closed_session_data():
    text = format_report(make_result(), Settings())
    assert "закрытие сессии 20.08.2026" in text


def test_empty_report_explains_why():
    text = format_report(make_result(ideas=[]), Settings())
    assert "Подходящих сетапов нет" in text
    assert "10:1" in text
    assert "не сбой сканера" in text


def test_report_includes_funnel_and_warnings():
    result = make_result(errors=["Yahoo вернул 429"])
    text = format_report(result, Settings())
    assert "Воронка отбора" in text
    assert "вселенная 4200" in text
    assert "Yahoo вернул 429" in text


def test_console_output_has_no_html_tags():
    text = format_console(make_result(), Settings())
    assert "<b>" not in text and "</b>" not in text and "<i>" not in text


def test_split_message_respects_telegram_limit():
    ideas = [make_idea(symbol=f"SYM{i}") for i in range(40)]
    text = format_report(make_result(ideas=ideas), Settings())
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)
    assert sum(len(c) for c in chunks) >= len(text) - len(chunks)


def test_split_message_keeps_html_tags_intact():
    """Разрез идёт по строкам, поэтому теги не рвутся пополам."""
    ideas = [make_idea(symbol=f"SYM{i}") for i in range(40)]
    for chunk in split_message(format_report(make_result(ideas=ideas), Settings())):
        assert chunk.count("<b>") == chunk.count("</b>")


def test_split_message_short_text_untouched():
    assert split_message("короткий текст") == ["короткий текст"]


def test_split_message_truncates_overlong_line():
    chunks = split_message("x" * 5000, limit=100)
    assert all(len(c) <= 100 for c in chunks)


def test_save_report_writes_json(tmp_path):
    settings = Settings().with_overrides(reports_dir=tmp_path)
    path = save_report(make_result(), settings)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ideas"][0]["symbol"] == "AAPL"
    assert data["ideas"][0]["setup_title"]
    assert data["funnel"]["universe"] == 4200
    assert data["as_of"] == "2026-08-20"


def test_data_outage_is_reported_as_failure_not_as_no_setups():
    """Полный отказ источника данных нельзя выдавать за «сетапов нет»."""
    result = make_result(
        ideas=[],
        funnel=FunnelStats(universe=5000, with_data=0),
        errors=["Не удалось загрузить котировки ни по одной бумаге"],
    )
    text = format_report(result, Settings())
    assert "Нет рыночных данных" in text
    assert "Подходящих сетапов нет" not in text


def test_console_output_unescapes_entities():
    text = format_console(make_result(ideas=[make_idea(name="AT&T Inc")]), Settings())
    assert "AT&T" in text
    assert "&amp;" not in text
