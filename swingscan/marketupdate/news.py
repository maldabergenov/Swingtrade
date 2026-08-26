"""Блок 2 — заголовки финансовых изданий за последние 12–16 часов.

Источники — открытые RSS (CNBC, MarketWatch, WSJ, FT) плюс Google News для
Reuters и Bloomberg, у которых публичных лент не осталось. Если задан
``ALPHAVANTAGE_API_KEY``, дополнительно берём NEWS_SENTIMENT — там чистые
таймстемпы и разметка по темам.

Отбор: окно по времени -> дедупликация по нормализованному заголовку ->
ранжирование по ключевым словам (макро/ставки/геополитика важнее корпоратива)
-> квота на источник, чтобы одна лента не заняла весь отчёт.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from .config import MarketUpdateConfig
from .feeds import FetchError, http_get, parse_datetime, parse_feed, strip_html, within_window
from .models import Headline, NewsSection

log = logging.getLogger(__name__)

ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"
SECTION_TITLE = "Главные новости"

# Максимум заголовков от одного издания — иначе быстрая лента вытеснит остальные.
PER_SOURCE_QUOTA = 2

_NORMALIZE_RE = re.compile(r"[^a-z0-9а-яё ]+")
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "is", "its", "by"}
)

# Тематические метки — используются, когда пересказ через LLM недоступен.
_TAGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ставки", ("fed", "fomc", "powell", "rate", "yield", "treasury", "ecb", "boj")),
    ("инфляция", ("inflation", "cpi", "ppi", "pce", "price index")),
    ("макро", ("gdp", "payroll", "jobs", "unemployment", "recession", "retail sales")),
    ("геополитика", ("war", "sanction", "tariff", "trade war", "election", "strike")),
    ("сырьё", ("oil", "crude", "opec", "gold", "copper", "gas")),
    ("отчётности", ("earnings", "guidance", "revenue", "profit", "outlook")),
    ("крипто", ("bitcoin", "crypto", "ethereum")),
)


def collect_news(
    config: MarketUpdateConfig, *, session: Any | None = None, now: datetime | None = None
) -> NewsSection:
    """Собирает и ранжирует заголовки; частичный сбой источников не критичен."""
    moment = now or datetime.now(timezone.utc)
    if not config.feeds and not (config.news_use_alphavantage and config.alphavantage_key):
        return NewsSection.failed("news", SECTION_TITLE, "в конфиге не задано ни одной ленты")

    collected: list[Headline] = []
    used: list[str] = []
    failed: list[str] = []

    for feed in config.feeds:
        try:
            response = http_get(
                feed.url,
                timeout=config.http_timeout,
                headers=config.headers(),
                session=session,
            )
            items = within_window(parse_feed(response.text), config.lookback_hours, now=moment)
        except FetchError as exc:
            log.warning("Лента %s недоступна: %s", feed.name, exc)
            failed.append(f"{feed.name} ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - битый XML не должен ронять блок
            log.warning("Лента %s: ошибка разбора (%s)", feed.name, exc)
            failed.append(f"{feed.name} (разбор)")
            continue

        log.info("Лента %s: %d записей в окне %dч", feed.name, len(items), config.lookback_hours)
        if items:
            used.append(feed.name)
        collected.extend(
            Headline(
                title=item.title,
                url=_clean_url(item.url),
                source=feed.name,
                published_at=item.published_at,
                summary=item.summary[:400],
            )
            for item in items
        )

    if config.news_use_alphavantage and config.alphavantage_key:
        try:
            extra = _fetch_alphavantage(config, moment, session=session)
            if extra:
                used.append("Alpha Vantage")
                collected.extend(extra)
            log.info("Alpha Vantage: %d новостей в окне", len(extra))
        except FetchError as exc:
            log.warning("Alpha Vantage News недоступен: %s", exc)
            failed.append(f"Alpha Vantage ({exc})")

    if not collected:
        return NewsSection.failed("news", SECTION_TITLE, _failure_reason(failed))

    ranked = rank_headlines(collected, config, now=moment)
    section = NewsSection(key="news", title=SECTION_TITLE)
    section.headlines = ranked
    section.sources_used = sorted(set(used))
    section.sources_failed = failed
    log.info("Новости: отобрано %d из %d заголовков", len(ranked), len(collected))
    return section


def rank_headlines(
    headlines: Iterable[Headline], config: MarketUpdateConfig, *, now: datetime | None = None
) -> list[Headline]:
    """Дедуплицирует, оценивает и режет до ``max_headlines`` с квотой на источник."""
    moment = now or datetime.now(timezone.utc)
    unique: dict[str, Headline] = {}
    for headline in headlines:
        key = _normalize(headline.title)
        if not key:
            continue
        scored = Headline(
            title=headline.title,
            url=headline.url,
            source=headline.source,
            published_at=headline.published_at,
            summary=headline.summary,
            score=_score(headline, config, moment),
            tag=_tag(headline.title, headline.summary),
        )
        current = unique.get(key)
        if current is None or scored.score > current.score:
            unique[key] = scored

    ordered = sorted(unique.values(), key=lambda h: (-h.score, h.source))
    picked: list[Headline] = []
    per_source: dict[str, int] = {}
    # Первый проход — с квотой; второй добирает остаток, если материала мало.
    for headline in ordered:
        if len(picked) >= config.max_headlines:
            break
        if per_source.get(headline.source, 0) >= PER_SOURCE_QUOTA:
            continue
        per_source[headline.source] = per_source.get(headline.source, 0) + 1
        picked.append(headline)
    for headline in ordered:
        if len(picked) >= config.max_headlines:
            break
        if headline not in picked:
            picked.append(headline)
    return picked


def _failure_reason(failed: list[str]) -> str:
    """Компактная причина: сколько лент не ответило и пример ошибки.

    Перечислять все восемь лент с полным текстом ошибки бессмысленно —
    при отказе сети причина у всех одна.
    """
    if not failed:
        return "за последние часы подходящих новостей не найдено"
    if len(failed) <= 2:
        return "; ".join(failed)
    return f"не ответило источников: {len(failed)} (например, {failed[0]})"


# ------------------------------------------------------------------ детали
def _score(headline: Headline, config: MarketUpdateConfig, now: datetime) -> float:
    haystack = f"{headline.title} {headline.summary}".lower()
    score = 0.0
    for keyword in config.priority_high:
        if keyword.lower() in haystack:
            score += 3.0
    for keyword in config.priority_medium:
        if keyword.lower() in haystack:
            score += 1.0
    # Свежесть: линейный бонус, полностью затухающий к концу окна.
    if headline.published_at is not None:
        age_hours = max(0.0, (now - headline.published_at).total_seconds() / 3600.0)
        score += max(0.0, 2.0 * (1.0 - age_hours / max(1, config.lookback_hours)))
    return score


def _tag(title: str, summary: str) -> str:
    haystack = f"{title} {summary}".lower()
    for label, keywords in _TAGS:
        if any(keyword in haystack for keyword in keywords):
            return label
    return "рынки"


def _normalize(title: str) -> str:
    """Ключ дедупликации: одна новость приходит в разных лентах разными словами."""
    lowered = _NORMALIZE_RE.sub(" ", title.lower())
    words = [word for word in lowered.split() if word not in _STOPWORDS]
    return " ".join(sorted(words)[:8])


def _clean_url(url: str) -> str:
    """Google News прячет исходную ссылку в параметре url — достаём её."""
    if "news.google.com" not in url:
        return url
    target = parse_qs(urlparse(url).query).get("url")
    return target[0] if target else url


def _fetch_alphavantage(
    config: MarketUpdateConfig, now: datetime, *, session: Any | None
) -> list[Headline]:
    time_from = (now - timedelta(hours=config.lookback_hours)).strftime("%Y%m%dT%H%M")
    response = http_get(
        ALPHAVANTAGE_URL,
        timeout=config.http_timeout,
        headers=config.headers(),
        params={
            "function": "NEWS_SENTIMENT",
            "topics": "economy_macro,financial_markets,economy_monetary",
            "time_from": time_from,
            "sort": "LATEST",
            "limit": 50,
            "apikey": config.alphavantage_key,
        },
        session=session,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError(f"некорректный JSON: {exc}") from exc

    limit_note = payload.get("Note") or payload.get("Information")
    if limit_note:
        raise FetchError(str(limit_note)[:160])

    feed = payload.get("feed")
    if not isinstance(feed, list):
        raise FetchError("ответ без поля feed")

    headlines: list[Headline] = []
    for item in feed:
        if not isinstance(item, dict):
            continue
        title = strip_html(str(item.get("title", "")))
        url = str(item.get("url", ""))
        if not title or not url:
            continue
        headlines.append(
            Headline(
                title=title,
                url=url,
                source=str(item.get("source", "Alpha Vantage")) or "Alpha Vantage",
                published_at=parse_datetime(_av_time(str(item.get("time_published", "")))),
                summary=strip_html(str(item.get("summary", "")))[:400],
            )
        )
    return headlines


def _av_time(raw: str) -> str:
    """Alpha Vantage отдаёт '20260826T034500' — приводим к ISO."""
    if len(raw) != 15 or "T" not in raw:
        return ""
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}+00:00"
