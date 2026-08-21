"""Формирование отчёта для Telegram и файлов на диске."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .backtest import breakeven_hit_rate
from .config import Settings
from .models import ScanResult, TradeIdea

TELEGRAM_LIMIT = 4096
SETUP_EMOJI = {"BO": "🚀", "MOM": "⚡", "PB": "🔄"}


def _money(value: float) -> str:
    """Компактная запись сумм: 1.2B / 340M / 5.6M."""
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:.0f}"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def format_idea(idea: TradeIdea, position: int) -> str:
    emoji = SETUP_EMOJI.get(idea.setup_code, "📈")
    risk_per_share = idea.entry - idea.stop
    lines = [
        f"<b>{position}. {_esc(idea.symbol)}</b> — {_esc(idea.name[:44])}  "
        f"<b>{idea.score:.0f}/100</b>",
        f"{emoji} {_esc(idea.setup_title)}",
        f"Цена: <b>${idea.last_close:.2f}</b>",
        f"Вход: <b>${idea.entry:.2f}</b> (стоп-заявка выше уровня)",
        f"Стоп: <b>${idea.stop:.2f}</b> (−{idea.risk_pct * 100:.2f}%, "
        f"${risk_per_share:.2f} на акцию)",
        f"Цель: <b>${idea.target:.2f}</b> (+{idea.reward_pct * 100:.1f}%)",
        f"R:R = <b>{idea.rr:.1f}:1</b> · держать {idea.min_hold_days}–"
        f"{idea.max_hold_days} сессий, до {idea.deadline:%d.%m}",
        f"ATR {idea.atr_pct * 100:.1f}% · ADX {idea.adx:.0f} · RSI {idea.rsi:.0f} · "
        f"RS(3м) {idea.rs_63d * 100:+.0f}%",
        f"Оборот {_money(idea.avg_dollar_volume)}/день · кап. {_money(idea.market_cap)}",
    ]
    if idea.backtest.signals:
        bt = idea.backtest
        lines.append(
            f"История сетапа: {bt.signals} сигн., {bt.hit_rate * 100:.0f}% успеха, "
            f"матожидание {bt.expectancy_r:+.2f}R"
        )
    if idea.reasons:
        bullets = "\n".join(f"  • {_esc(reason)}" for reason in idea.reasons[:5])
        lines.append(bullets)
    return "\n".join(lines)


def format_header(result: ScanResult, settings: Settings) -> str:
    tz = ZoneInfo(settings.timezone)
    local = result.started_at.astimezone(tz)
    spec = settings.trade
    title = "🇺🇸 <b>Swing-сканер рынка США</b>"
    when = f"{local:%d.%m.%Y %H:%M} ({settings.timezone})"
    data_note = (
        f"данные: внутридневные, бар {result.as_of:%d.%m} ещё формируется"
        if result.partial_session and result.as_of
        else f"данные: закрытие сессии {result.as_of:%d.%m.%Y}"
        if result.as_of
        else "данные: нет"
    )
    criteria = (
        f"Критерии: цена ≥ ${settings.filters.min_price:.0f} · "
        f"кап. ≥ {_money(settings.filters.min_market_cap)} · "
        f"цель ≥ +{spec.min_target_pct * 100:.0f}% · "
        f"риск ≤ {spec.max_risk_pct * 100:.0f}% · "
        f"{spec.min_hold_days}–{spec.max_hold_days} сессий"
    )
    return f"{title}\n{when}\n{_esc(data_note)}\n{_esc(criteria)}"


def format_funnel(result: ScanResult) -> str:
    f = result.funnel
    return (
        "📊 <b>Воронка отбора</b>\n"
        f"вселенная {f.universe} → с историей {f.with_data} → "
        f"цена {f.passed_price} → ликвидность {f.passed_liquidity} → "
        f"капитализация {f.passed_market_cap} → сетап {f.passed_setup} → "
        f"риск/цель {f.passed_risk_reward} → реализуемость {f.passed_feasibility} → "
        f"история {f.passed_backtest}"
    )


def format_report(result: ScanResult, settings: Settings) -> str:
    parts = [format_header(result, settings)]

    if result.ideas:
        parts.append(f"\n<b>Найдено идей: {len(result.ideas)}</b>")
        for position, idea in enumerate(result.ideas, start=1):
            parts.append("\n" + format_idea(idea, position))
    elif result.funnel.universe > 0 and result.funnel.with_data == 0:
        parts.append(
            "\n🛑 <b>Нет рыночных данных.</b>\n"
            "Котировки не загрузились ни по одной бумаге — это сбой доступа к "
            "источнику данных, а не отсутствие сетапов. Проверьте интернет и "
            "доступность Yahoo Finance, затем запустите скан повторно."
        )
    else:
        parts.append(
            "\n❌ <b>Подходящих сетапов нет.</b>\n"
            "Связка «цель +{target:.0f}% при риске {risk:.0f}%» требует R:R "
            "{rr:.0f}:1 — такие конфигурации появляются редко. "
            "Пустой отчёт означает, что рынок сегодня не даёт сделок с нужным "
            "перевесом, а не сбой сканера.".format(
                target=settings.trade.min_target_pct * 100,
                risk=settings.trade.max_risk_pct * 100,
                rr=settings.trade.min_rr,
            )
        )

    parts.append("\n" + format_funnel(result))
    parts.append(
        "\nℹ️ Порог безубыточности при таком R:R — "
        f"{breakeven_hit_rate(settings.trade) * 100:.0f}% успешных сделок. "
        f"Скан занял {result.duration_seconds:.0f} с."
    )
    if result.errors:
        errors = "\n".join(f"  • {_esc(e)}" for e in result.errors[:5])
        parts.append(f"\n⚠️ <b>Предупреждения</b>\n{errors}")
    parts.append(
        "\n<i>Не инвестиционная рекомендация. Проверяйте отчётности и новости "
        "перед входом.</i>"
    )
    return "\n".join(parts)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет сообщение по границам блоков, не разрывая строки."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        piece = line if len(line) <= limit else line[:limit]
        addition = len(piece) + (1 if current else 0)
        if current_len + addition > limit:
            chunks.append("\n".join(current))
            current, current_len = [piece], len(piece)
        else:
            current.append(piece)
            current_len += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def save_report(result: ScanResult, settings: Settings) -> Path:
    """Сохраняет полный JSON-отчёт (аудит и последующий анализ)."""
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.started_at.astimezone(ZoneInfo(settings.timezone))
    path = settings.reports_dir / f"scan_{stamp:%Y%m%d_%H%M}.json"
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def format_console(result: ScanResult, settings: Settings) -> str:
    """Человекочитаемый вывод в терминал (без HTML-разметки)."""
    import re

    text = re.sub(r"</?(b|i|code|pre)>", "", format_report(result, settings))
    return html.unescape(text)


def now_local(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))
