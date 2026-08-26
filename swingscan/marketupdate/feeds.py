"""HTTP-хелпер и разбор RSS/Atom без внешних зависимостей.

``feedparser`` сюда не тянем: лент немного, а форматы у них ровно два —
RSS 2.0 и Atom. Разбор идёт стандартным ``xml.etree``, дата приводится к
timezone-aware UTC, чтобы окно «последние N часов» считалось корректно.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

# Пространства имён Atom и Dublin Core, встречающиеся в лентах изданий.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
}
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class FetchError(RuntimeError):
    pass


def short_error(exc: Exception) -> str:
    """Короткая причина сбоя.

    Трассировки requests тянут на несколько сотен символов (весь URL, вся цепочка
    прокси) и в Telegram выглядят как мусор. В отчёт нужен класс ошибки, полный
    текст остаётся в логе Actions.
    """
    name = type(exc).__name__
    message = " ".join(str(exc).split())
    # Внутренние скобки requests: берём только первое предложение.
    message = message.split(" (Caused by", 1)[0].split(":", 1)[0]
    return f"{name}" if not message or message == name else f"{name}: {message[:60]}"


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    published_at: datetime | None
    summary: str


def http_get(
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    session: Any | None = None,
) -> requests.Response:
    """GET с понятной ошибкой вместо голого исключения requests."""
    caller = session or requests
    try:
        response = caller.get(url, timeout=timeout, headers=headers, params=params)
    except Exception as exc:  # noqa: BLE001 - сеть падает по-разному
        log.debug("GET %s: %s", url, exc)
        raise FetchError(short_error(exc)) from exc
    status = getattr(response, "status_code", 0)
    if status != 200:
        raise FetchError(f"HTTP {status}")
    return response


def strip_html(text: str) -> str:
    """Аннотации в лентах приходят с разметкой — чистим и схлопываем пробелы."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def parse_datetime(value: str | None) -> datetime | None:
    """RFC 822 (RSS) и ISO 8601 (Atom) -> aware datetime в UTC."""
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        candidate = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(xml_text: str) -> list[FeedItem]:
    """Разбирает RSS 2.0 или Atom в плоский список записей."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise FetchError(f"некорректный XML: {exc}") from exc

    items = [_rss_item(node) for node in root.iter("item")]
    items += [_atom_entry(node) for node in root.iter(f"{{{_NS['atom']}}}entry")]
    return [item for item in items if item is not None and item.title and item.url]


def within_window(
    items: Iterable[FeedItem], hours: int, *, now: datetime | None = None
) -> list[FeedItem]:
    """Оставляет записи за последние ``hours`` часов.

    Записи без даты сохраняем: часть лент (в первую очередь Google News)
    отдаёт их нерегулярно, а терять свежую новость из-за этого не хочется.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(hours=hours)
    # Небольшой допуск вперёд: у некоторых лент часы уходят на минуты в будущее.
    horizon = moment + timedelta(hours=2)
    return [
        item
        for item in items
        if item.published_at is None or cutoff <= item.published_at <= horizon
    ]


# ------------------------------------------------------------------ разбор
def _text(node: Any, *paths: str) -> str:
    for path in paths:
        found = node.find(path, _NS) if ":" in path else node.find(path)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return ""


def _rss_item(node: Any) -> FeedItem | None:
    title = strip_html(_text(node, "title"))
    url = _text(node, "link", "guid")
    published = parse_datetime(_text(node, "pubDate", "dc:date"))
    summary = strip_html(_text(node, "description", "media:description"))
    if not title or not url:
        return None
    return FeedItem(title=title, url=url, published_at=published, summary=summary)


def _atom_entry(node: Any) -> FeedItem | None:
    title = strip_html(_text(node, "atom:title"))
    url = ""
    for link in node.findall("atom:link", _NS):
        rel = link.get("rel", "alternate")
        if rel == "alternate" and link.get("href"):
            url = link.get("href", "")
            break
    if not url:
        url = _text(node, "atom:id")
    published = parse_datetime(_text(node, "atom:published", "atom:updated"))
    summary = strip_html(_text(node, "atom:summary", "atom:content"))
    if not title or not url:
        return None
    return FeedItem(title=title, url=url, published_at=published, summary=summary)
