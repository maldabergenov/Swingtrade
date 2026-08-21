"""Командный интерфейс swingscan."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from . import __version__
from .app import run_once
from .config import Settings
from .scheduler import next_run_times, serve
from .telegram import TelegramClient, TelegramError


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%d.%m %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swingscan",
        description=(
            "Сканер рынка акций США для swing-сделок: цель ≥ +20%, риск ≤ 2%, "
            "горизонт 2–3 недели. Отчёты уходят в Telegram дважды в день."
        ),
    )
    parser.add_argument("--version", action="version", version=f"swingscan {__version__}")
    parser.add_argument("--env-file", help="путь к .env (по умолчанию .env в корне)")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="ограничить вселенную N бумагами (для быстрой отладки)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="разовое сканирование прямо сейчас")
    scan.add_argument(
        "--dry-run", action="store_true", help="не отправлять в Telegram, только вывод"
    )
    scan.add_argument("--no-cache", action="store_true", help="игнорировать кэш котировок")

    serve_cmd = sub.add_parser("serve", help="демон с расписанием 09:00 и 21:00")
    serve_cmd.add_argument(
        "--run-now", action="store_true", help="выполнить скан сразу при старте"
    )

    sub.add_parser("schedule", help="показать ближайшие запуски по расписанию")

    telegram_cmd = sub.add_parser("telegram-test", help="проверить связь с ботом")
    telegram_cmd.add_argument("--text", default=None, help="текст тестового сообщения")

    universe_cmd = sub.add_parser("universe", help="показать размер вселенной")
    universe_cmd.add_argument("--refresh", action="store_true", help="обновить кэш")

    return parser


def _settings_from_args(args: argparse.Namespace) -> Settings:
    settings = Settings.load(args.env_file)
    if args.log_level:
        settings = settings.with_overrides(log_level=args.log_level)
    if args.limit is not None:
        settings = settings.with_overrides(
            filters=replace(settings.filters, max_symbols=args.limit)
        )
    if getattr(args, "no_cache", False):
        settings = settings.with_overrides(data=replace(settings.data, use_cache=False))
    return settings


def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    result = run_once(settings, dry_run=args.dry_run)
    return 0 if not result.errors else 1


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    serve(settings, run_immediately=args.run_now)
    return 0


def cmd_schedule(settings: Settings, _args: argparse.Namespace) -> int:
    print(f"Часовой пояс: {settings.timezone}")
    print(f"Время сканов: {', '.join(settings.scan_times)}")
    for moment in next_run_times(settings):
        if moment:
            print(f"  ближайший запуск: {moment:%d.%m.%Y %H:%M %Z} (UTC {moment.utcoffset()})")
    return 0


def cmd_telegram_test(settings: Settings, args: argparse.Namespace) -> int:
    try:
        client = TelegramClient(settings.telegram, state_dir=settings.state_dir)
        me = client.get_me()
        print(f"Бот: @{me.get('username', '?')} (id {me.get('id', '?')})")
        chat_id = client.resolve_chat_id()
        print(f"chat_id: {chat_id}")
        text = args.text or (
            "✅ <b>swingscan подключён</b>\nОтчёты будут приходить в "
            f"{' и '.join(settings.scan_times)} ({settings.timezone})."
        )
        client.send_message(text, chat_id)
        print("Тестовое сообщение отправлено.")
        return 0
    except TelegramError as exc:
        print(f"Ошибка Telegram: {exc}", file=sys.stderr)
        return 1


def cmd_universe(settings: Settings, args: argparse.Namespace) -> int:
    from .universe import fetch_universe

    symbols = fetch_universe(
        exclude_etf=settings.filters.exclude_etf,
        cache_dir=settings.data.cache_dir,
        cache_ttl_hours=0 if args.refresh else 12,
        retries=settings.data.request_retries,
    )
    print(f"Бумаг во вселенной: {len(symbols)}")
    for info in symbols[:10]:
        print(f"  {info.yahoo_symbol:<8} {info.exchange:<14} {info.name[:50]}")
    if len(symbols) > 10:
        print(f"  ... и ещё {len(symbols) - 10}")
    return 0


COMMANDS = {
    "scan": cmd_scan,
    "serve": cmd_serve,
    "schedule": cmd_schedule,
    "telegram-test": cmd_telegram_test,
    "universe": cmd_universe,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings_from_args(args)
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    return COMMANDS[args.command](settings, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
