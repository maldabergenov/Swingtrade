#!/usr/bin/env python3
"""Точка входа ежедневного маркет-апдейта.

    python market_update.py             # собрать и отправить в Telegram
    python market_update.py --dry-run   # только вывод в консоль

Логика живёт в пакете ``swingscan.marketupdate``; здесь только запуск, чтобы
workflow и Makefile ссылались на короткий путь.
"""

from __future__ import annotations

import sys

from swingscan.marketupdate.cli import main

if __name__ == "__main__":
    sys.exit(main())
