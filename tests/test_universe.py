from __future__ import annotations

import pytest

from swingscan.universe import (
    SymbolInfo,
    _looks_like_common_stock,
    _post_filter,
    fetch_universe,
    load_fallback_universe,
    parse_nasdaq_listed,
    parse_other_listed,
    to_yahoo_symbol,
)

NASDAQ_SAMPLE = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|Q|N|N|100|Y|N
ZZZZT|Nasdaq Test Stock|G|Y|N|100|N|N
ABCDW|Some Company - Warrant|S|N|N|100|N|N
XYZU|Some Company - Unit|S|N|N|100|N|N
LONGSYM|Too Long Symbol|S|N|N|100|N|N
File Creation Time: 0821202602:30|||||
"""

OTHER_SAMPLE = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.A|Berkshire Hathaway Inc. Class A Common Stock|N|BRK.A|N|100|N|BRK.A
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
BAC$B|Bank of America Depositary Shares|N|BAC$B|N|100|N|BAC-PB
TSTX|Test Issue|A|TSTX|N|100|Y|TSTX
GE|General Electric Company|N|GE|N|100|N|GE
File Creation Time: 0821202602:30||||||
"""


def test_parse_nasdaq_listed_keeps_common_stock_only():
    symbols = {s.symbol for s in parse_nasdaq_listed(NASDAQ_SAMPLE)}
    assert "AAPL" in symbols
    assert "QQQ" in symbols  # ETF помечается флагом, отсев — на следующем шаге
    assert "ZZZZT" not in symbols  # тестовый выпуск
    assert "ABCDW" not in symbols  # варрант
    assert "XYZU" not in symbols  # юнит
    assert "LONGSYM" not in symbols  # слишком длинный тикер


def test_parse_nasdaq_listed_marks_etf():
    by_symbol = {s.symbol: s for s in parse_nasdaq_listed(NASDAQ_SAMPLE)}
    assert by_symbol["QQQ"].is_etf is True
    assert by_symbol["AAPL"].is_etf is False
    assert by_symbol["AAPL"].exchange == "NASDAQ"


def test_parse_other_listed_maps_exchange_and_symbols():
    by_symbol = {s.symbol: s for s in parse_other_listed(OTHER_SAMPLE)}
    assert by_symbol["BRK.A"].yahoo_symbol == "BRK-A"
    assert by_symbol["BRK.A"].exchange == "NYSE"
    assert by_symbol["SPY"].exchange == "NYSE Arca"
    assert by_symbol["SPY"].is_etf is True
    assert "BAC$B" not in by_symbol  # привилегированные отсеиваются
    assert "TSTX" not in by_symbol  # тестовый выпуск


def test_parsers_tolerate_garbage_input():
    assert parse_nasdaq_listed("") == []
    assert parse_other_listed("совсем не тот формат") == []
    assert parse_nasdaq_listed("Symbol|Security Name\nAAPL|Apple") == []


@pytest.mark.parametrize(
    "symbol,name,expected",
    [
        ("AAPL", "Apple Inc. - Common Stock", True),
        ("BRK.A", "Berkshire Hathaway Class A", True),
        ("ABC", "ABC Corp 7.5% Notes due 2050", False),
        ("DEF", "DEF Inc Rights", False),
        ("GHI", "GHI Preferred Series A", False),
        ("JK$L", "Whatever", False),
        ("TOOLONG", "Whatever", False),
    ],
)
def test_looks_like_common_stock(symbol, name, expected):
    assert _looks_like_common_stock(symbol, name) is expected


def test_to_yahoo_symbol():
    assert to_yahoo_symbol("brk.b") == "BRK-B"
    assert to_yahoo_symbol(" aapl ") == "AAPL"


def test_post_filter_drops_etf_and_duplicates():
    items = [
        SymbolInfo("AAPL", "Apple", "NASDAQ", False),
        SymbolInfo("AAPL", "Apple duplicate", "NASDAQ", False),
        SymbolInfo("SPY", "SPDR", "NYSE Arca", True),
    ]
    result = _post_filter(items, exclude_etf=True)
    assert [s.symbol for s in result] == ["AAPL"]
    assert len(_post_filter(items, exclude_etf=False)) == 2


def test_fallback_universe_is_shipped_and_sane():
    symbols = load_fallback_universe()
    assert len(symbols) > 300
    tickers = {s.symbol for s in symbols}
    assert {"AAPL", "MSFT", "NVDA"} <= tickers
    assert len(tickers) == len(symbols)  # без дубликатов


def test_fetch_universe_falls_back_when_network_fails(monkeypatch, tmp_path):
    def boom(*_args, **_kwargs):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr("swingscan.universe._download", boom)
    symbols = fetch_universe(cache_dir=tmp_path)
    assert len(symbols) > 300  # использован резервный список
    assert not (tmp_path / "universe.txt").exists()  # мусор в кэш не пишем


def test_fetch_universe_uses_and_writes_cache(monkeypatch, tmp_path):
    calls = {"count": 0}

    def fake_download(url, timeout, retries):
        calls["count"] += 1
        return NASDAQ_SAMPLE if "nasdaqlisted" in url else OTHER_SAMPLE

    monkeypatch.setattr("swingscan.universe._download", fake_download)
    first = fetch_universe(cache_dir=tmp_path)
    assert calls["count"] == 2
    assert (tmp_path / "universe.txt").exists()

    second = fetch_universe(cache_dir=tmp_path)
    assert calls["count"] == 2  # второй раз сеть не трогаем
    assert [s.symbol for s in first] == [s.symbol for s in second]
