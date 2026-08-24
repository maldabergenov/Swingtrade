"""Загрузка дневных котировок и фундаментальных данных через Yahoo Finance.

Стратегия обращения к API:

1. Дневные бары качаются пачками (``yf.download`` на список тикеров) — это на
   порядок быстрее поштучных запросов и щадит лимиты Yahoo.
2. Капитализация запрашивается только для бумаг, переживших фильтры по цене и
   ликвидности: это десятки-сотни запросов вместо тысяч.
3. Результат кэшируется на диск с TTL, чтобы повторный запуск в тот же день не
   ходил в сеть заново.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pandas as pd

log = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")


def _import_yfinance():  # pragma: no cover - тонкая обёртка над импортом
    import yfinance as yf

    return yf


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    market_cap: float
    shares_outstanding: float = 0.0


def chunked(items: Sequence[str], size: int) -> Iterable[list[str]]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def normalize_history(raw: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Приводит выдачу yfinance к таблице OHLCV с DatetimeIndex.

    ``yf.download`` для нескольких тикеров возвращает MultiIndex-колонки, для
    одного — плоские. Функция обрабатывает оба случая и оба порядка уровней.
    """
    if raw is None or len(raw) == 0:
        return None

    frame = raw
    if isinstance(frame.columns, pd.MultiIndex):
        level0 = set(frame.columns.get_level_values(0))
        level1 = set(frame.columns.get_level_values(1))
        if symbol in level0:
            frame = frame.xs(symbol, axis=1, level=0)
        elif symbol in level1:
            frame = frame.xs(symbol, axis=1, level=1)
        else:
            return None

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        return None

    frame = frame.loc[:, list(REQUIRED_COLUMNS)].copy()
    frame = frame.apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame["Volume"] = frame["Volume"].fillna(0.0)
    if frame.empty:
        return None

    frame.index = pd.DatetimeIndex(frame.index)
    if frame.index.tz is not None:
        frame.index = frame.index.tz_localize(None)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    # Отбрасываем бары с нулевой/отрицательной ценой — битые данные Yahoo.
    frame = frame[(frame[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return frame if not frame.empty else None


class MarketData:
    """Фасад над yfinance с пачечной загрузкой и дисковым кэшем."""

    def __init__(
        self,
        *,
        period: str = "2y",
        batch_size: int = 120,
        retries: int = 3,
        retry_backoff: float = 2.0,
        cache_dir: Path | None = None,
        cache_ttl_minutes: int = 180,
        use_cache: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        downloader: Callable[..., pd.DataFrame] | None = None,
        quote_fetcher: Callable[[Sequence[str]], dict[str, Quote]] | None = None,
    ) -> None:
        self.period = period
        self.batch_size = batch_size
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl_minutes = cache_ttl_minutes
        self.use_cache = use_cache and self.cache_dir is not None
        self._sleep = sleep
        self._downloader = downloader
        self._quote_fetcher = quote_fetcher

    # ------------------------------------------------------------------ бары
    def download_history(self, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
        """Возвращает {тикер: OHLCV}. Отсутствующие бумаги просто пропускаются."""
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}

        cached = self._read_cache(symbols) if self.use_cache else {}
        pending = [s for s in symbols if s not in cached]
        log.info(
            "История: %d из кэша, %d к загрузке", len(cached), len(pending)
        )

        result: dict[str, pd.DataFrame] = dict(cached)
        for batch in chunked(pending, self.batch_size):
            frames = self._download_batch(batch)
            result.update(frames)
            if self.use_cache:
                self._write_cache(frames)
        return result

    def _download_batch(self, batch: list[str]) -> dict[str, pd.DataFrame]:
        raw = None
        for attempt in range(self.retries):
            try:
                raw = self._raw_download(batch)
                break
            except Exception as exc:  # noqa: BLE001
                wait = self.retry_backoff * (2**attempt)
                log.warning(
                    "Пачка из %d тикеров не загрузилась (попытка %d): %s; жду %.0fс",
                    len(batch),
                    attempt + 1,
                    exc,
                    wait,
                )
                if attempt + 1 < self.retries:
                    self._sleep(wait)
        if raw is None:
            return {}

        out: dict[str, pd.DataFrame] = {}
        for symbol in batch:
            frame = normalize_history(raw, symbol)
            if frame is not None:
                out[symbol] = frame
        return out

    def _raw_download(self, batch: list[str]) -> pd.DataFrame:
        if self._downloader is not None:
            return self._downloader(batch)
        yf = _import_yfinance()
        return yf.download(
            tickers=batch,
            period=self.period,
            interval="1d",
            auto_adjust=True,
            actions=False,
            group_by="ticker",
            threads=True,
            progress=False,
            repair=False,
        )

    # ------------------------------------------------------- фундаментальные
    def fetch_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Цена и капитализация. Ошибка по одной бумаге не рушит пачку."""
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        if self._quote_fetcher is not None:
            return self._quote_fetcher(symbols)
        return self._fetch_quotes_yf(symbols)

    def _fetch_quotes_yf(self, symbols: Sequence[str]) -> dict[str, Quote]:  # pragma: no cover - сеть
        yf = _import_yfinance()
        out: dict[str, Quote] = {}
        tickers = yf.Tickers(" ".join(symbols))
        for symbol in symbols:
            try:
                ticker = tickers.tickers.get(symbol) or yf.Ticker(symbol)
                info = ticker.fast_info
                price = _safe_float(_fast_info_get(info, "last_price"))
                market_cap = _safe_float(_fast_info_get(info, "market_cap"))
                shares = _safe_float(_fast_info_get(info, "shares"))
                if market_cap <= 0 and price > 0 and shares > 0:
                    market_cap = price * shares
                out[symbol] = Quote(symbol, price, market_cap, shares)
            except Exception as exc:  # noqa: BLE001
                log.debug("Нет фундаментальных данных по %s: %s", symbol, exc)
        return out

    # ---------------------------------------------------------------- кэш
    def _cache_path(self, symbol: str) -> Path:
        assert self.cache_dir is not None
        safe = symbol.replace("/", "_").replace("\\", "_")
        return self.cache_dir / "history" / f"{safe}.csv"

    def _read_cache(self, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        if self.cache_dir is None:
            return out
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.cache_ttl_minutes)
        for symbol in symbols:
            path = self._cache_path(symbol)
            if not path.exists():
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
            try:
                frame = pd.read_csv(path, index_col=0, parse_dates=True)
            except Exception:  # noqa: BLE001
                continue
            normalized = normalize_history(frame, symbol)
            if normalized is not None:
                out[symbol] = normalized
        return out

    def _write_cache(self, frames: dict[str, pd.DataFrame]) -> None:
        if self.cache_dir is None:
            return
        (self.cache_dir / "history").mkdir(parents=True, exist_ok=True)
        for symbol, frame in frames.items():
            try:
                frame.to_csv(self._cache_path(symbol))
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось записать кэш %s: %s", symbol, exc)


def _fast_info_get(info: object, key: str) -> object:
    """fast_info в разных версиях yfinance ведёт себя как dict или как объект."""
    try:
        return info[key]  # type: ignore[index]
    except Exception:  # noqa: BLE001
        return getattr(info, key, None)


def _safe_float(value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(result) or math.isinf(result):
        return 0.0
    return result
