"""CLI маркет-апдейта: ``python market_update.py`` или ``python -m swingscan.marketupdate``."""

from __future__ import annotations

import argparse
import sys

from ..cli import configure_logging
from .config import MarketUpdateConfig, MarketUpdateConfigError
from .app import run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market-update",
        description=(
            "Ежедневный маркет апдейт в Telegram: итоги прошлой сессии США, "
            "главные заголовки финансовых изданий и посты из X."
        ),
    )
    parser.add_argument("--config", help="путь к TOML (по умолчанию config/market_update.toml)")
    parser.add_argument("--env-file", help="путь к .env (по умолчанию .env в корне)")
    parser.add_argument("--log-level", help="DEBUG/INFO/WARNING/ERROR")
    parser.add_argument(
        "--dry-run", action="store_true", help="не отправлять в Telegram, только вывод"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = MarketUpdateConfig.load(args.config, args.env_file)
    except MarketUpdateConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    configure_logging(args.log_level or config.log_level)
    update = run_once(config, dry_run=args.dry_run)
    # Ненулевой код только если не собрался ни один из трёх блоков:
    # частичный отчёт — штатная ситуация, ронять workflow из-за неё не нужно.
    return 1 if update.all_failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
