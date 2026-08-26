"""Блок 1 — итоги прошлой торговой сессии.

Два провайдера котировок, пробуются по порядку из конфига:

``yfinance``
    Уже есть в зависимостях сканера, знает настоящие индексы (^GSPC, ^IXIC,
    ^NDX, ^DJI) и умеет забирать все тикеры одним батч-запросом.
``alphavantage``
    Требует ``ALPHAVANTAGE_API_KEY``. Индексов у него нет, поэтому символы
    подменяются ETF-прокси (SPY/QQQ/DIA) — на дневных процентах разница
    незначительная. Бесплатный тариф ограничен, запросы идут поштучно.

Если оба провайдера не дали ни одной котировки, блок помечается как
временно недоступный, а отчёт всё равно уходит.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from .config import InstrumentSpec, MarketUpdateConfig
from .feeds import FetchError, http_get
from .models import MarketSection, QuoteChange

log = logging.getLogger(__name__)

ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
SECTION_TITLE = "Итоги прошлой сессии"


def collect_market(
    config: MarketUpdateConfig, *, session: Any | None = None
) -> MarketSection:
    """Собирает индексы и секторы, перебирая провайдеров до первого успеха."""
    instruments = list(config.indices) + list(config.sectors)
    if not instruments:
        return MarketSection.failed(
            "market", SECTION_TITLE, "в конфиге не задано ни одного инструмента"
        )

    problems: list[str] = []
    for provider in config.market_providers:
        if provider == "alphavantage" and not config.alphavantage_key:
            problems.append("alphavantage: нет ALPHAVANTAGE_API_KEY")
            continue
        log.info("Котировки: пробую провайдера %s", provider)
        try:
            quotes = _fetch(provider, instruments, config, session=session)
        except Exception as exc:  # noqa: BLE001 - провайдер не должен ронять отчёт
            log.warning("Котировки: провайдер %s не отработал (%s)", provider, exc)
            problems.append(f"{provider}: {' '.join(str(exc).split())[:80]}")
            continue

        indices = _pick(config.indices, quotes, provider)
        sectors = _pick(config.sectors, quotes, provider)
        if not indices and not sectors:
            log.warning("Котировки: провайдер %s вернул пустой результат", provider)
            problems.append(f"{provider}: пустой ответ")
            continue

        log.info(
            "Котировки: провайдер %s дал %d индексов и %d секторов",
            provider,
            len(indices),
            len(sectors),
        )
        session_date = next(
            (q.session_date for q in indices + sectors if q.session_date), ""
        )
        section = MarketSection(
            key="market", title=SECTION_TITLE, provider=provider, session_date=session_date
        )
        section.indices = indices
        section.sectors = sectors
        section.context = describe_drivers(section)
        return section

    reason = "; ".join(problems) or "источники котировок недоступны"
    return MarketSection.failed("market", SECTION_TITLE, reason)


def describe_drivers(section: MarketSection) -> str:
    """Запасной контекст «что двигало рынок» без обращения к LLM.

    Строится из фактов, которые уже есть: направление широкого рынка,
    лидирующий и отстающий секторы, единодушие движения по секторам.
    """
    if not section.indices and not section.sectors:
        return ""

    broad = next(
        (q for q in section.indices if q.symbol in ("^GSPC", "SPY")), None
    ) or (section.indices[0] if section.indices else None)
    parts: list[str] = []
    if broad is not None:
        if broad.change_pct > 0.75:
            mood = "уверенный рост широкого рынка"
        elif broad.change_pct > 0.1:
            mood = "умеренный рост широкого рынка"
        elif broad.change_pct < -0.75:
            mood = "заметная распродажа"
        elif broad.change_pct < -0.1:
            mood = "умеренное снижение"
        else:
            mood = "рынок закрылся практически без движения"
        parts.append(f"{broad.title}: {mood} ({broad.change_pct:+.2f}%).")

    if section.sectors:
        up = sum(1 for q in section.sectors if q.change_pct > 0)
        total = len(section.sectors)
        leader, laggard = section.gainers[0], section.losers[0]
        breadth = (
            "движение широкое — росло большинство секторов"
            if up >= total * 0.7
            else "падало большинство секторов"
            if up <= total * 0.3
            else "движение разнонаправленное по секторам"
        )
        parts.append(
            f"Вёл {leader.title} ({leader.change_pct:+.2f}%), "
            f"отставал {laggard.title} ({laggard.change_pct:+.2f}%); {breadth}."
        )
    return " ".join(parts)


# ---------------------------------------------------------------- провайдеры
def _fetch(
    provider: str,
    instruments: Sequence[InstrumentSpec],
    config: MarketUpdateConfig,
    *,
    session: Any | None,
) -> dict[str, QuoteChange]:
    if provider == "yfinance":
        return _fetch_yfinance(instruments, config)
    if provider == "alphavantage":
        return _fetch_alphavantage(instruments, config, session=session)
    raise FetchError(f"неизвестный провайдер котировок {provider!r}")


def _pick(
    specs: Sequence[InstrumentSpec], quotes: dict[str, QuoteChange], provider: str
) -> list[QuoteChange]:
    """Сопоставляет спеки из конфига с полученными котировками."""
    picked: list[QuoteChange] = []
    for spec in specs:
        quote = quotes.get(spec.symbol_for(provider).upper())
        if quote is None:
            log.debug("Нет котировки для %s (%s)", spec.symbol, provider)
            continue
        # Возвращаем исходный символ и название из конфига, а не прокси-тикер.
        picked.append(
            QuoteChange(
                symbol=spec.symbol,
                title=spec.title,
                close=quote.close,
                change_pct=quote.change_pct,
                previous_close=quote.previous_close,
                session_date=quote.session_date,
            )
        )
    return picked


def _fetch_yfinance(
    instruments: Sequence[InstrumentSpec], config: MarketUpdateConfig
) -> dict[str, QuoteChange]:
    """Один батч-запрос на все тикеры: две последние дневные свечи."""
    import yfinance as yf

    symbols = sorted({spec.symbol_for("yfinance") for spec in instruments})
    raw = yf.download(
        tickers=symbols,
        period="7d",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    if raw is None or len(raw) == 0:
        raise FetchError("yfinance вернул пустую таблицу")

    from ..market_data import normalize_history

    quotes: dict[str, QuoteChange] = {}
    for symbol in symbols:
        frame = normalize_history(raw, symbol)
        if frame is None or len(frame) < 2:
            continue
        closes = frame["Close"]
        last, previous = float(closes.iloc[-1]), float(closes.iloc[-2])
        if previous <= 0:
            continue
        quotes[symbol.upper()] = QuoteChange(
            symbol=symbol,
            title=symbol,
            close=last,
            change_pct=(last / previous - 1.0) * 100.0,
            previous_close=previous,
            session_date=f"{frame.index[-1]:%d.%m.%Y}",
        )
    if not quotes:
        raise FetchError("не удалось разобрать ни одной серии")
    return quotes


def _fetch_alphavantage(
    instruments: Sequence[InstrumentSpec],
    config: MarketUpdateConfig,
    *,
    session: Any | None,
) -> dict[str, QuoteChange]:
    """GLOBAL_QUOTE поштучно: у Alpha Vantage нет батч-эндпоинта на free tier."""
    symbols = sorted({spec.symbol_for("alphavantage") for spec in instruments})
    quotes: dict[str, QuoteChange] = {}
    limit_hit = ""

    for symbol in symbols:
        try:
            response = http_get(
                ALPHAVANTAGE_URL,
                timeout=config.http_timeout,
                headers=config.headers(),
                params={
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol,
                    "apikey": config.alphavantage_key,
                },
                session=session,
            )
            payload = response.json()
        except (FetchError, ValueError) as exc:
            log.debug("Alpha Vantage %s: %s", symbol, exc)
            continue

        # Превышение лимита приходит с HTTP 200 и полем Note/Information.
        limit_note = payload.get("Note") or payload.get("Information")
        if limit_note:
            limit_hit = str(limit_note)[:160]
            log.warning("Alpha Vantage: %s", limit_hit)
            break

        quote = _parse_alphavantage_quote(symbol, payload.get("Global Quote") or {})
        if quote is not None:
            quotes[symbol.upper()] = quote

    if not quotes:
        raise FetchError(limit_hit or "нет данных по запрошенным тикерам")
    return quotes


def _parse_alphavantage_quote(symbol: str, block: dict[str, Any]) -> QuoteChange | None:
    try:
        close = float(block["05. price"])
        previous = float(block["08. previous close"])
    except (KeyError, TypeError, ValueError):
        return None
    if previous <= 0:
        return None
    raw_day = str(block.get("07. latest trading day", ""))
    day = "-".join(reversed(raw_day.split("-"))).replace("-", ".") if raw_day else ""
    return QuoteChange(
        symbol=symbol,
        title=symbol,
        close=close,
        change_pct=(close / previous - 1.0) * 100.0,
        previous_close=previous,
        session_date=day,
    )
