"""Формирование текста маркет-апдейта для Telegram.

Разметка — HTML (тот же ``parse_mode``, что у сканера). Нарезка по лимиту
4096 символов делегирована ``swingscan.report.split_message``: тот же код,
что режет отчёты сканера, и та же гарантия — строки не рвутся посередине.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import MarketUpdateConfig
from .models import MarketUpdate, QuoteChange, Section

# Мягкий потолок на секцию, чтобы длинный блок не вытеснял остальные.
MAX_RETELLING_CHARS = 200
MAX_POST_CHARS = 220


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _arrow(change_pct: float) -> str:
    if change_pct > 0.05:
        return "🟢"
    if change_pct < -0.05:
        return "🔴"
    return "⚪️"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,.;:—-") + "…"


def format_quote(quote: QuoteChange) -> str:
    return (
        f"{_arrow(quote.change_pct)} <b>{_esc(quote.title)}</b> "
        f"{quote.close:,.2f} ({quote.change_pct:+.2f}%)".replace(",", " ")
    )


def format_unavailable(section: Section) -> str:
    """Единый вид для упавшего блока — отчёт не должен молча терять раздел."""
    lines = [f"⚠️ <b>{_esc(section.title)}</b> — раздел временно недоступен."]
    if section.reason:
        lines.append(f"Причина: {_esc(_clip(section.reason, 220))}")
    if section.hint:
        lines.append(f"Что сделать: {_esc(section.hint)}")
    return "\n".join(lines)


def format_header(update: MarketUpdate, config: MarketUpdateConfig) -> str:
    local = update.generated_at.astimezone(ZoneInfo(config.timezone))
    return (
        "🌅 <b>Маркет апдейт</b>\n"
        f"{local:%d.%m.%Y %H:%M} ({config.timezone})"
    )


def format_market(update: MarketUpdate) -> str:
    section = update.market
    if not section.ok:
        return format_unavailable(section)

    when = f" за {_esc(section.session_date)}" if section.session_date else ""
    lines = [f"📊 <b>Итоги сессии{when}</b>"]
    lines.extend(format_quote(quote) for quote in section.indices)

    if section.sectors:
        gainers = " · ".join(
            f"{_esc(q.title)} {q.change_pct:+.2f}%" for q in section.gainers
        )
        losers = " · ".join(
            f"{_esc(q.title)} {q.change_pct:+.2f}%" for q in section.losers
        )
        lines.append(f"\n📈 <b>Лидеры:</b> {gainers}")
        lines.append(f"📉 <b>Аутсайдеры:</b> {losers}")

    if section.context:
        lines.append(f"\n{_esc(_clip(section.context, 320))}")
    return "\n".join(lines)


def format_news(update: MarketUpdate) -> str:
    section = update.news
    if not section.ok:
        return format_unavailable(section)

    lines = [f"📰 <b>Новости за последние часы</b> ({len(section.headlines)})"]
    for position, headline in enumerate(section.headlines, start=1):
        body = headline.retelling or f"[{headline.tag}] {headline.title}"
        lines.append(
            f"{position}. {_esc(_clip(body, MAX_RETELLING_CHARS))} "
            f"<a href=\"{_esc(headline.url)}\">{_esc(headline.source)}</a>"
        )
    if section.sources_failed:
        failed = ", ".join(section.sources_failed[:4])
        lines.append(f"<i>Не ответили: {_esc(_clip(failed, 180))}</i>")
    return "\n".join(lines)


def format_social(update: MarketUpdate) -> str:
    section = update.social
    if not section.ok:
        return format_unavailable(section)

    lines = [f"🐦 <b>Twitter / X</b> ({section.provider})"]
    for position, post in enumerate(section.posts, start=1):
        body = post.retelling or post.text
        likes = f" · ♥ {post.likes}" if post.likes else ""
        lines.append(
            f"{position}. <b>@{_esc(post.author)}</b>{likes} — "
            f"{_esc(_clip(body, MAX_POST_CHARS))} "
            f"<a href=\"{_esc(post.url)}\">пост</a>"
        )
    return "\n".join(lines)


def format_footer(update: MarketUpdate) -> str:
    lines: list[str] = []
    if update.warnings:
        notes = "; ".join(update.warnings[:3])
        lines.append(f"<i>ℹ️ {_esc(_clip(notes, 240))}</i>")
    lines.append(
        "<i>Не инвестиционная рекомендация. Заголовки пересказаны кратко — "
        "проверяйте первоисточник по ссылке.</i>"
    )
    return "\n".join(lines)


def format_update(update: MarketUpdate, config: MarketUpdateConfig) -> str:
    parts = [
        format_header(update, config),
        format_market(update),
        format_news(update),
        format_social(update),
        format_footer(update),
    ]
    return "\n\n".join(part for part in parts if part)


def format_console(update: MarketUpdate, config: MarketUpdateConfig) -> str:
    """Тот же отчёт без HTML — для вывода в лог GitHub Actions."""
    text = re.sub(r"<a href=\"[^\"]*\">([^<]*)</a>", r"\1", format_update(update, config))
    text = re.sub(r"</?(b|i|code|pre)>", "", text)
    return html.unescape(text)


def save_update(update: MarketUpdate, config: MarketUpdateConfig) -> Path:
    """Сохраняет JSON-снимок отчёта — для артефакта Actions и разбора полётов."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = update.generated_at.astimezone(ZoneInfo(config.timezone))
    path = config.reports_dir / f"market_update_{stamp:%Y%m%d_%H%M}.json"
    path.write_text(
        json.dumps(update.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def now_local(config: MarketUpdateConfig) -> datetime:
    return datetime.now(ZoneInfo(config.timezone))
