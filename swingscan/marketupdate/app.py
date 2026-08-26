"""Оркестрация маркет-апдейта: собрать три блока, пересказать, отправить.

Ключевое свойство — блок не роняет отчёт. Каждый сборщик изолирован
``try/except``: упавший источник превращается в секцию с пометкой
«раздел временно недоступен», остальные два уходят в Telegram как обычно.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..telegram import TelegramClient, TelegramError
from .config import MarketUpdateConfig
from .market import SECTION_TITLE as MARKET_TITLE
from .market import collect_market
from .models import MarketSection, MarketUpdate, NewsSection, SocialSection
from .news import SECTION_TITLE as NEWS_TITLE
from .news import collect_news
from .report import format_console, format_update, save_update
from .social import SECTION_TITLE as SOCIAL_TITLE
from .social import collect_social
from .summarize import enrich

log = logging.getLogger(__name__)


def build_update(
    config: MarketUpdateConfig,
    *,
    session: Any | None = None,
    now: datetime | None = None,
    llm_client: Any | None = None,
) -> MarketUpdate:
    """Собирает отчёт целиком, переживая падение любого из источников."""
    moment = now or datetime.now(timezone.utc)

    log.info("Блок 1/3: итоги торговой сессии")
    market = _safe(
        lambda: collect_market(config, session=session),
        MarketSection,
        "market",
        MARKET_TITLE,
    )

    log.info("Блок 2/3: новости за последние %d часов", config.lookback_hours)
    news = _safe(
        lambda: collect_news(config, session=session, now=moment),
        NewsSection,
        "news",
        NEWS_TITLE,
    )

    log.info("Блок 3/3: Twitter / X (провайдер %s)", config.x_provider)
    social = _safe(
        lambda: collect_social(config, session=session, now=moment),
        SocialSection,
        "social",
        SOCIAL_TITLE,
    )

    update = MarketUpdate(generated_at=moment, market=market, news=news, social=social)
    log.info(
        "Блоки собраны: рынок=%s, новости=%s, X=%s",
        "ok" if market.ok else "нет",
        "ok" if news.ok else "нет",
        "ok" if social.ok else "нет",
    )
    return enrich(update, config, client=llm_client)


def deliver(
    update: MarketUpdate,
    config: MarketUpdateConfig,
    *,
    dry_run: bool = False,
    client: TelegramClient | None = None,
) -> bool:
    """Печатает отчёт в лог, сохраняет JSON и отправляет в Telegram."""
    print(format_console(update, config))

    try:
        path = save_update(update, config)
        log.info("Снимок отчёта сохранён: %s", path)
    except Exception as exc:  # noqa: BLE001 - сохранение не блокирует отправку
        log.warning("Не удалось сохранить снимок отчёта: %s", exc)

    if dry_run:
        log.info("Режим --dry-run: в Telegram не отправляю")
        return False
    if client is None and not config.telegram.enabled:
        log.warning("TELEGRAM_BOT_TOKEN не задан — отчёт только в консоль и файл")
        return False

    try:
        if client is None:
            client = TelegramClient(config.telegram, state_dir=config.state_dir)
        # send_message сам режет текст по лимиту Telegram в 4096 символов.
        chunks = client.send_message(format_update(update, config))
        log.info("Маркет апдейт отправлен в Telegram (%d сообщ.)", len(chunks))
        return True
    except TelegramError as exc:
        log.error("Telegram: %s", exc)
        return False


def run_once(
    config: MarketUpdateConfig,
    *,
    dry_run: bool = False,
    session: Any | None = None,
    client: TelegramClient | None = None,
    llm_client: Any | None = None,
) -> MarketUpdate:
    config.ensure_dirs()
    started = datetime.now(timezone.utc)
    log.info("Старт маркет-апдейта (%s)", started.isoformat(timespec="seconds"))
    update = build_update(config, session=session, now=started, llm_client=llm_client)
    deliver(update, config, dry_run=dry_run, client=client)
    log.info(
        "Готово за %.1f с",
        (datetime.now(timezone.utc) - started).total_seconds(),
    )
    return update


def _safe(collector: Any, section_cls: Any, key: str, title: str) -> Any:
    """Любое исключение сборщика превращается в помеченную секцию, а не в падение."""
    try:
        return collector()
    except Exception as exc:  # noqa: BLE001 - это и есть граница изоляции блока
        log.exception("Блок %s упал: %s", key, exc)
        return section_cls.failed(key, title, f"внутренняя ошибка: {exc}")
