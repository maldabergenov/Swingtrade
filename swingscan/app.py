"""Связка сканера и доставки: один запуск = скан + отчёт + отправка."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import Settings
from .models import ScanResult
from .report import format_console, format_report, save_report
from .scanner import Scanner
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)


def run_scan(settings: Settings, scanner: Scanner | None = None) -> ScanResult:
    settings.ensure_dirs()
    scanner = scanner or Scanner(settings)
    started = datetime.now(timezone.utc)
    log.info("Старт сканирования (%s)", started.isoformat(timespec="seconds"))
    result = scanner.run(started)
    log.info(
        "Скан завершён за %.0f с: %d идей из %d бумаг",
        result.duration_seconds,
        len(result.ideas),
        result.funnel.universe,
    )
    return result


def deliver(
    result: ScanResult,
    settings: Settings,
    *,
    dry_run: bool = False,
    client: TelegramClient | None = None,
) -> bool:
    """Печатает отчёт, сохраняет JSON и отправляет в Telegram.

    Возвращает True, если сообщение ушло в Telegram.
    """
    text = format_report(result, settings)
    print(format_console(result, settings))

    try:
        path = save_report(result, settings)
        log.info("Отчёт сохранён: %s", path)
    except Exception as exc:  # noqa: BLE001 - сохранение не должно блокировать отправку
        log.warning("Не удалось сохранить отчёт: %s", exc)

    if dry_run:
        log.info("Режим --dry-run: в Telegram не отправляю")
        return False
    if not result.ideas and not settings.send_empty_report:
        log.info("Идей нет, отправка пустого отчёта отключена")
        return False
    if client is None and not settings.telegram.enabled:
        log.warning("TELEGRAM_BOT_TOKEN не задан — отчёт только в консоль и файл")
        return False

    try:
        if client is None:
            client = TelegramClient(settings.telegram, state_dir=settings.state_dir)
        client.send_message(text)
        log.info("Отчёт отправлен в Telegram")
        return True
    except TelegramError as exc:
        log.error("Telegram: %s", exc)
        return False


def run_once(
    settings: Settings,
    *,
    dry_run: bool = False,
    scanner: Scanner | None = None,
    client: TelegramClient | None = None,
) -> ScanResult:
    result = run_scan(settings, scanner)
    deliver(result, settings, dry_run=dry_run, client=client)
    return result
