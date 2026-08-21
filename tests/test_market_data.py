from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swingscan.market_data import MarketData, Quote, chunked, normalize_history
from tests.synthetic import breakout_stock, multi_ticker_frame


def test_chunked_splits_evenly():
    assert list(chunked(list("abcde"), 2)) == [["a", "b"], ["c", "d"], ["e"]]
    assert list(chunked([], 10)) == []
    assert list(chunked(["a"], 0)) == [["a"]]


def test_normalize_history_handles_ticker_major_multiindex():
    frames = {"AAPL": breakout_stock(), "MSFT": breakout_stock(seed=9)}
    raw = multi_ticker_frame(frames)
    result = normalize_history(raw, "AAPL")
    assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(result) == len(frames["AAPL"])


def test_normalize_history_handles_field_major_multiindex():
    frame = breakout_stock()
    columns = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["AAPL"]])
    raw = pd.DataFrame(frame.to_numpy(), index=frame.index, columns=columns)
    result = normalize_history(raw, "AAPL")
    assert result is not None
    assert len(result) == len(frame)


def test_normalize_history_returns_none_for_unknown_symbol():
    raw = multi_ticker_frame({"AAPL": breakout_stock()})
    assert normalize_history(raw, "MSFT") is None
    assert normalize_history(pd.DataFrame(), "AAPL") is None
    assert normalize_history(None, "AAPL") is None


def test_normalize_history_drops_timezone_and_duplicates():
    idx = pd.DatetimeIndex(
        ["2024-01-02", "2024-01-02", "2024-01-03"], tz="America/New_York"
    )
    raw = pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [1.1, 2.1, 3.1],
            "Low": [0.9, 1.9, 2.9],
            "Close": [1.0, 2.0, 3.0],
            "Volume": [10, 20, 30],
        },
        index=idx,
    )
    result = normalize_history(raw, "AAPL")
    assert result.index.tz is None
    assert len(result) == 2
    assert result["Close"].iloc[0] == 2.0  # оставлена последняя запись за день


def test_normalize_history_drops_broken_and_missing_rows():
    raw = pd.DataFrame(
        {
            "Open": [10.0, np.nan, -1.0, 12.0],
            "High": [11.0, 11.0, 1.0, 13.0],
            "Low": [9.0, 9.0, 0.5, 11.0],
            "Close": [10.5, 10.0, 0.0, 12.5],
            "Volume": [1e6, 1e6, 1e6, np.nan],
        },
        index=pd.bdate_range("2024-01-01", periods=4),
    )
    result = normalize_history(raw, "X")
    assert len(result) == 2
    assert result["Volume"].iloc[-1] == 0.0


def test_download_history_batches_and_skips_missing():
    frames = {f"S{i}": breakout_stock(seed=i) for i in range(5)}
    seen: list[list[str]] = []

    def downloader(batch):
        seen.append(batch)
        return multi_ticker_frame({s: frames[s] for s in batch if s in frames})

    md = MarketData(batch_size=2, use_cache=False, downloader=downloader)
    result = md.download_history([*frames.keys(), "MISSING"])
    assert sorted(result) == sorted(frames)
    assert [len(b) for b in seen] == [2, 2, 2]


def test_download_history_deduplicates_symbols():
    calls: list[list[str]] = []

    def downloader(batch):
        calls.append(batch)
        return multi_ticker_frame({"AAPL": breakout_stock()})

    md = MarketData(use_cache=False, downloader=downloader)
    md.download_history(["AAPL", "AAPL", "", "AAPL"])
    assert calls == [["AAPL"]]


def test_download_history_retries_then_gives_up():
    attempts = {"count": 0}

    def downloader(_batch):
        attempts["count"] += 1
        raise RuntimeError("Yahoo не отвечает")

    md = MarketData(use_cache=False, retries=3, downloader=downloader, sleep=lambda _s: None)
    assert md.download_history(["AAPL"]) == {}
    assert attempts["count"] == 3


def test_download_history_recovers_after_transient_failure():
    attempts = {"count": 0}

    def downloader(batch):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("таймаут")
        return multi_ticker_frame({"AAPL": breakout_stock()})

    md = MarketData(use_cache=False, retries=3, downloader=downloader, sleep=lambda _s: None)
    assert "AAPL" in md.download_history(["AAPL"])


def test_cache_round_trip_avoids_second_download(tmp_path):
    calls = {"count": 0}

    def downloader(batch):
        calls["count"] += 1
        return multi_ticker_frame({"AAPL": breakout_stock()})

    kwargs = dict(cache_dir=tmp_path, use_cache=True, downloader=downloader)
    first = MarketData(**kwargs).download_history(["AAPL"])
    second = MarketData(**kwargs).download_history(["AAPL"])
    assert calls["count"] == 1
    pd.testing.assert_frame_equal(first["AAPL"], second["AAPL"], check_freq=False)


def test_expired_cache_is_refetched(tmp_path):
    calls = {"count": 0}

    def downloader(batch):
        calls["count"] += 1
        return multi_ticker_frame({"AAPL": breakout_stock()})

    MarketData(cache_dir=tmp_path, use_cache=True, downloader=downloader).download_history(["AAPL"])
    md = MarketData(cache_dir=tmp_path, use_cache=True, cache_ttl_minutes=0, downloader=downloader)
    md.download_history(["AAPL"])
    assert calls["count"] == 2


def test_fetch_quotes_uses_injected_provider():
    md = MarketData(
        use_cache=False,
        quote_fetcher=lambda symbols: {s: Quote(s, 10.0, 1e9) for s in symbols},
    )
    quotes = md.fetch_quotes(["AAPL", "AAPL", "MSFT"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert quotes["AAPL"].market_cap == pytest.approx(1e9)


def test_fetch_quotes_empty_input():
    assert MarketData(use_cache=False).fetch_quotes([]) == {}


def test_fast_info_get_supports_dict_and_attribute_access():
    from swingscan.market_data import _fast_info_get

    class AttrInfo:
        last_price = 12.5

    assert _fast_info_get({"last_price": 10.0}, "last_price") == 10.0
    assert _fast_info_get(AttrInfo(), "last_price") == 12.5
    assert _fast_info_get(object(), "market_cap") is None


@pytest.mark.parametrize(
    "value,expected",
    [(10, 10.0), ("12.5", 12.5), (None, 0.0), ("нет", 0.0), (float("nan"), 0.0), (float("inf"), 0.0)],
)
def test_safe_float(value, expected):
    from swingscan.market_data import _safe_float

    assert _safe_float(value) == expected


def test_write_cache_survives_unwritable_path(tmp_path):
    md = MarketData(cache_dir=tmp_path / "nested", use_cache=True, downloader=lambda b: None)
    md._write_cache({"BAD/SYM": breakout_stock()})  # не должно бросать исключение
