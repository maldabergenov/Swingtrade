"""Блок 3 — посты и треды из X (Twitter).

ВАЖНО ПРО ДОСТУП. Бесплатного X API больше не существует: чтение ленты
требует либо платного тарифа (X API v2, Basic и выше), либо стороннего
провайдера, либо публичного RSS-моста. Поэтому провайдер выбирается в
``config/market_update.toml`` -> ``[x].provider``, а ключ приходит из
GitHub Secrets:

===============  ==========================  ==============================
provider         секрет                      что это
===============  ==========================  ==============================
``none``         —                           блок выключен (по умолчанию)
``x_api``        ``X_BEARER_TOKEN``          официальный X API v2, тариф Basic+
``twitterapi_io````X_API_KEY``               сторонний twitterapi.io
``apidance``     ``X_API_KEY``               сторонний apidance.pro
``rss``          —                           мост Nitter/RSSHub, нужен только URL
===============  ==========================  ==============================

Если провайдер не настроен или упал, блок не роняет отчёт: секция уходит с
пометкой «временно недоступен» и подсказкой, какой ключ добавить.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import MarketUpdateConfig
from .feeds import FetchError, http_get, parse_datetime, parse_feed, strip_html, within_window
from .models import SocialPost, SocialSection

log = logging.getLogger(__name__)

SECTION_TITLE = "Twitter / X"
X_API_URL = "https://api.twitter.com/2/tweets/search/recent"
TWITTERAPI_IO_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
APIDANCE_URL = "https://api.apidance.pro/1.1/search/tweets.json"

# Подсказка, которая уходит прямо в Telegram, если блок не настроен.
HINTS = {
    "none": (
        "блок отключён: в config/market_update.toml задайте [x].provider "
        "(x_api / twitterapi_io / apidance / rss) и добавьте соответствующий секрет"
    ),
    "x_api": "добавьте секрет X_BEARER_TOKEN (X API v2, тариф Basic или выше)",
    "twitterapi_io": "добавьте секрет X_API_KEY (twitterapi.io)",
    "apidance": "добавьте секрет X_API_KEY (apidance.pro)",
    "rss": "задайте [x].rss_base_url — адрес инстанса Nitter или RSSHub",
}


def collect_social(
    config: MarketUpdateConfig, *, session: Any | None = None, now: datetime | None = None
) -> SocialSection:
    """Забирает посты выбранным провайдером; при любом сбое — мягкая деградация."""
    provider = config.x_provider
    moment = now or datetime.now(timezone.utc)

    if provider == "none":
        return _unavailable(provider, "прямой доступ к X не подключён")
    if not config.x_accounts:
        return _unavailable(provider, "в конфиге пустой список [x].accounts")
    if not config.x_credential:
        return _unavailable(provider, f"не задан доступ для провайдера {provider}")

    log.info("X: провайдер %s, аккаунтов %d", provider, len(config.x_accounts))
    try:
        posts = _fetch(provider, config, moment, session=session)
    except FetchError as exc:
        log.warning("X: провайдер %s недоступен (%s)", provider, exc)
        return _unavailable(provider, str(exc))
    except Exception as exc:  # noqa: BLE001 - сторонние API отвечают как угодно
        log.warning("X: провайдер %s вернул неожиданный ответ (%s)", provider, exc)
        return _unavailable(provider, f"неожиданный ответ: {exc}")

    filtered = [post for post in posts if post.likes >= config.x_min_likes]
    filtered.sort(key=lambda p: (p.likes, p.published_at or moment), reverse=True)
    picked = filtered[: config.max_tweets]
    if not picked:
        return _unavailable(provider, "за последние часы подходящих постов нет")

    log.info("X: отобрано %d постов из %d", len(picked), len(posts))
    section = SocialSection(key="social", title=SECTION_TITLE, provider=provider)
    section.posts = picked
    return section


def _unavailable(provider: str, reason: str) -> SocialSection:
    section = SocialSection.failed("social", SECTION_TITLE, reason, HINTS.get(provider, ""))
    section.provider = provider
    return section


# ---------------------------------------------------------------- адаптеры
def _fetch(
    provider: str, config: MarketUpdateConfig, now: datetime, *, session: Any | None
) -> list[SocialPost]:
    if provider == "x_api":
        return _fetch_x_api(config, session=session)
    if provider == "twitterapi_io":
        return _fetch_twitterapi_io(config, session=session)
    if provider == "apidance":
        return _fetch_apidance(config, session=session)
    if provider == "rss":
        return _fetch_rss(config, now, session=session)
    raise FetchError(f"неизвестный провайдер {provider!r}")


def _query(accounts: Sequence[str], limit: int = 20) -> str:
    """`from:a OR from:b ...` — X ограничивает длину запроса, режем список."""
    clause = " OR ".join(f"from:{handle}" for handle in accounts[:limit])
    return f"({clause}) -is:retweet -is:reply"


def _fetch_x_api(config: MarketUpdateConfig, *, session: Any | None) -> list[SocialPost]:
    """Официальный X API v2, GET /2/tweets/search/recent."""
    headers = dict(config.headers())
    headers["Authorization"] = f"Bearer {config.x_bearer_token}"
    response = http_get(
        X_API_URL,
        timeout=config.http_timeout,
        headers=headers,
        params={
            "query": _query(config.x_accounts),
            "max_results": max(10, min(100, config.max_tweets * 5)),
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        },
        session=session,
    )
    payload = _json(response)
    users = {
        str(user.get("id")): str(user.get("username", ""))
        for user in (payload.get("includes") or {}).get("users", [])
        if isinstance(user, dict)
    }
    posts: list[SocialPost] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        author = users.get(str(item.get("author_id")), "x")
        tweet_id = str(item.get("id", ""))
        metrics = item.get("public_metrics") or {}
        posts.append(
            SocialPost(
                author=author,
                text=strip_html(str(item.get("text", ""))),
                url=f"https://x.com/{author}/status/{tweet_id}",
                published_at=parse_datetime(str(item.get("created_at", ""))),
                likes=int(metrics.get("like_count", 0) or 0),
            )
        )
    return posts


def _fetch_twitterapi_io(config: MarketUpdateConfig, *, session: Any | None) -> list[SocialPost]:
    """twitterapi.io: тот же синтаксис запроса, ключ в заголовке X-API-Key."""
    headers = dict(config.headers())
    headers["X-API-Key"] = config.x_api_key
    response = http_get(
        TWITTERAPI_IO_URL,
        timeout=config.http_timeout,
        headers=headers,
        params={"query": _query(config.x_accounts), "queryType": "Latest"},
        session=session,
    )
    payload = _json(response)
    return _from_generic(payload.get("tweets") or payload.get("data") or [])


def _fetch_apidance(config: MarketUpdateConfig, *, session: Any | None) -> list[SocialPost]:
    """apidance.pro повторяет старый Twitter v1.1 search/tweets."""
    headers = dict(config.headers())
    headers["apikey"] = config.x_api_key
    response = http_get(
        APIDANCE_URL,
        timeout=config.http_timeout,
        headers=headers,
        params={"q": _query(config.x_accounts), "count": config.max_tweets * 5, "result_type": "recent"},
        session=session,
    )
    payload = _json(response)
    return _from_generic(payload.get("statuses") or payload.get("tweets") or [])


def _fetch_rss(
    config: MarketUpdateConfig, now: datetime, *, session: Any | None
) -> list[SocialPost]:
    """Мост Nitter/RSSHub: по ленте на аккаунт, часть хостов может лежать."""
    base = config.x_rss_base_url.rstrip("/")
    posts: list[SocialPost] = []
    failures = 0
    for handle in config.x_accounts:
        url = f"{base}/{handle}/rss" if "nitter" in base or base.endswith("/rss") else f"{base}/{handle}"
        try:
            response = http_get(
                url, timeout=config.http_timeout, headers=config.headers(), session=session
            )
            items = within_window(parse_feed(response.text), config.lookback_hours, now=now)
        except Exception as exc:  # noqa: BLE001 - мосты падают поодиночке
            log.debug("X RSS %s: %s", handle, exc)
            failures += 1
            continue
        posts.extend(
            SocialPost(
                author=handle,
                text=item.summary or item.title,
                url=item.url,
                published_at=item.published_at,
            )
            for item in items
        )
    if not posts and failures:
        raise FetchError(f"мост не ответил ни по одному из {failures} аккаунтов")
    return posts


def _from_generic(items: Any) -> list[SocialPost]:
    """Общий разбор для сторонних API — ключи у них похожи, но не идентичны."""
    posts: list[SocialPost] = []
    if not isinstance(items, list):
        return posts
    for item in items:
        if not isinstance(item, dict):
            continue
        author_block = item.get("author") or item.get("user") or {}
        author = str(
            (author_block.get("userName") or author_block.get("screen_name") or "")
            if isinstance(author_block, dict)
            else ""
        ) or str(item.get("username", "x"))
        tweet_id = str(item.get("id") or item.get("id_str") or "")
        text = strip_html(str(item.get("text") or item.get("full_text") or ""))
        if not text:
            continue
        posts.append(
            SocialPost(
                author=author,
                text=text,
                url=str(item.get("url") or f"https://x.com/{author}/status/{tweet_id}"),
                published_at=parse_datetime(str(item.get("createdAt") or item.get("created_at") or "")),
                likes=int(item.get("likeCount") or item.get("favorite_count") or 0),
            )
        )
    return posts


def _json(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError(f"некорректный JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FetchError("ожидался JSON-объект")
    errors = payload.get("errors") or payload.get("error")
    if errors and not (payload.get("data") or payload.get("tweets") or payload.get("statuses")):
        raise FetchError(str(errors)[:160])
    return payload
