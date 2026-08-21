"""Формирование вселенной инструментов — акции США с бирж NASDAQ/NYSE/AMEX.

Источник — публичные справочники NASDAQ Trader (обновляются ежедневно):

    https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
    https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

Если источник недоступен, используется вшитый резервный список ликвидных бумаг,
чтобы сканер не оставался без данных.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

FALLBACK_FILE = Path(__file__).resolve().parent / "data" / "fallback_universe.txt"

# Бумаги, которые не подходят для swing-торговли акциями.
_BAD_NAME_PATTERNS = re.compile(
    r"\b(warrant|warrants|unit|units|right|rights|preferred|depositary|depository|"
    r"debenture|note|notes|trust preferred|convertible|when issued|liquidating|"
    r"subordinated|etn|exchange[- ]traded note)\b",
    re.IGNORECASE,
)
_BAD_SYMBOL_CHARS = re.compile(r"[$^]")
# Суффиксы NASDAQ для варрантов/юнитов/прав/привилегированных.
_BAD_SUFFIX = re.compile(r"^[A-Z]{1,4}(W|WS|U|R|RT|P[A-Z]?)$")

EXCHANGE_NAMES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
    "Q": "NASDAQ",
    "G": "NASDAQ",
    "S": "NASDAQ",
}


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    name: str
    exchange: str
    is_etf: bool

    @property
    def yahoo_symbol(self) -> str:
        return to_yahoo_symbol(self.symbol)


def to_yahoo_symbol(symbol: str) -> str:
    """BRK.A -> BRK-A: Yahoo использует дефис для классов акций."""
    return symbol.strip().upper().replace(".", "-")


def _looks_like_common_stock(symbol: str, name: str) -> bool:
    if not symbol or _BAD_SYMBOL_CHARS.search(symbol):
        return False
    if len(symbol) > 5:
        return False
    if _BAD_NAME_PATTERNS.search(name):
        return False
    if len(symbol) == 5 and _BAD_SUFFIX.match(symbol):
        # 5-буквенные тикеры NASDAQ с суффиксом W/U/R/P — не обыкновенные акции.
        return False
    return True


def parse_nasdaq_listed(text: str) -> list[SymbolInfo]:
    """Разбирает nasdaqlisted.txt (Symbol|Security Name|...|Test Issue|...|ETF|...)."""
    out: list[SymbolInfo] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = [h.strip() for h in lines[0].split("|")]
    try:
        idx_symbol = header.index("Symbol")
        idx_name = header.index("Security Name")
        idx_test = header.index("Test Issue")
        idx_etf = header.index("ETF")
    except ValueError:
        log.warning("Неожиданный формат nasdaqlisted.txt")
        return out

    for line in lines[1:]:
        if line.startswith("File Creation Time") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) <= max(idx_symbol, idx_name, idx_test, idx_etf):
            continue
        if parts[idx_test].strip().upper() == "Y":
            continue
        symbol = parts[idx_symbol].strip().upper()
        name = parts[idx_name].strip()
        if not _looks_like_common_stock(symbol, name):
            continue
        out.append(
            SymbolInfo(
                symbol=symbol,
                name=name,
                exchange="NASDAQ",
                is_etf=parts[idx_etf].strip().upper() == "Y",
            )
        )
    return out


def parse_other_listed(text: str) -> list[SymbolInfo]:
    """Разбирает otherlisted.txt (NYSE, NYSE American, Arca и прочие площадки)."""
    out: list[SymbolInfo] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = [h.strip() for h in lines[0].split("|")]
    try:
        idx_symbol = header.index("ACT Symbol")
        idx_name = header.index("Security Name")
        idx_exchange = header.index("Exchange")
        idx_etf = header.index("ETF")
        idx_test = header.index("Test Issue")
    except ValueError:
        log.warning("Неожиданный формат otherlisted.txt")
        return out

    for line in lines[1:]:
        if line.startswith("File Creation Time") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) <= max(idx_symbol, idx_name, idx_exchange, idx_etf, idx_test):
            continue
        if parts[idx_test].strip().upper() == "Y":
            continue
        symbol = parts[idx_symbol].strip().upper()
        name = parts[idx_name].strip()
        if not _looks_like_common_stock(symbol, name):
            continue
        exchange_code = parts[idx_exchange].strip().upper()
        out.append(
            SymbolInfo(
                symbol=symbol,
                name=name,
                exchange=EXCHANGE_NAMES.get(exchange_code, exchange_code or "US"),
                is_etf=parts[idx_etf].strip().upper() == "Y",
            )
        )
    return out


def load_fallback_universe() -> list[SymbolInfo]:
    """Резервный список на случай недоступности справочников NASDAQ."""
    if not FALLBACK_FILE.exists():
        return []
    out: list[SymbolInfo] = []
    for line in FALLBACK_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbol, _, name = line.partition(",")
        out.append(
            SymbolInfo(
                symbol=symbol.strip().upper(),
                name=name.strip() or symbol.strip().upper(),
                exchange="US",
                is_etf=False,
            )
        )
    return out


def _download(url: str, timeout: float, retries: int) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "swingscan/1.0 (+market scanner)"},
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001 - логируем и пробуем ещё раз
            last_error = exc
            log.warning("Не удалось скачать %s (попытка %d): %s", url, attempt + 1, exc)
    if last_error:
        raise last_error
    return ""


def fetch_universe(
    *,
    exclude_etf: bool = True,
    cache_dir: Path | None = None,
    cache_ttl_hours: int = 12,
    timeout: float = 30.0,
    retries: int = 3,
) -> list[SymbolInfo]:
    """Возвращает список торгуемых акций США с кэшированием на диск."""
    cache_file = Path(cache_dir) / "universe.txt" if cache_dir else None
    if cache_file and cache_file.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            cache_file.stat().st_mtime, tz=timezone.utc
        )
        if age < timedelta(hours=cache_ttl_hours):
            symbols = _read_cache(cache_file)
            if symbols:
                log.info("Вселенная загружена из кэша: %d бумаг", len(symbols))
                return _post_filter(symbols, exclude_etf)

    symbols: list[SymbolInfo] = []
    for url, parser in ((NASDAQ_LISTED_URL, parse_nasdaq_listed), (OTHER_LISTED_URL, parse_other_listed)):
        try:
            symbols.extend(parser(_download(url, timeout, retries)))
        except Exception as exc:  # noqa: BLE001
            log.error("Источник %s недоступен: %s", url, exc)

    if not symbols:
        log.warning("Справочники NASDAQ недоступны, использую резервный список")
        symbols = load_fallback_universe()
    elif cache_file:
        _write_cache(cache_file, symbols)

    return _post_filter(symbols, exclude_etf)


def _post_filter(symbols: list[SymbolInfo], exclude_etf: bool) -> list[SymbolInfo]:
    seen: set[str] = set()
    out: list[SymbolInfo] = []
    for info in symbols:
        if exclude_etf and info.is_etf:
            continue
        key = info.yahoo_symbol
        if key in seen:
            continue
        seen.add(key)
        out.append(info)
    out.sort(key=lambda s: s.symbol)
    return out


def _write_cache(path: Path, symbols: list[SymbolInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{s.symbol}|{s.name}|{s.exchange}|{'Y' if s.is_etf else 'N'}" for s in symbols
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _read_cache(path: Path) -> list[SymbolInfo]:
    out: list[SymbolInfo] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        out.append(SymbolInfo(parts[0], parts[1], parts[2], parts[3] == "Y"))
    return out
