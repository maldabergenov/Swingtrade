"""Ежедневный маркет-апдейт: итоги сессии, новости, посты X — в Telegram.

Пакет намеренно отделён от сканера сетапов: у него своё расписание
(09:00 Астаны по будням), свои источники данных и своя деградация при сбоях.
Общее со сканером — только доставка (``swingscan.telegram``) и секреты.
"""

from __future__ import annotations

from .config import MarketUpdateConfig
from .models import MarketUpdate, Section

__all__ = ["MarketUpdateConfig", "MarketUpdate", "Section"]
