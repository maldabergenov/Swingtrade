"""Конфигурация сканера: пороги отбора, параметры сделки, расписание, Telegram.

Все значения читаются из переменных окружения (или из файла .env), значения по
умолчанию соответствуют техническому заданию:

    цена >= $5, капитализация >= $50M, потенциал >= +20%, риск на стоп <= 2%,
    горизонт удержания 2-3 недели.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:  # python-dotenv не обязателен для работы, только для удобства
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - зависит от окружения

    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Казахстан с 01.03.2024 живёт в едином часовом поясе UTC+5 без перехода на
# летнее время, поэтому 09:00 Астаны == 04:00 UTC, 21:00 Астаны == 16:00 UTC.
DEFAULT_TIMEZONE = "Asia/Almaty"
DEFAULT_SCAN_TIMES = "09:00,21:00"


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace("_", "").replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Некорректное числовое значение {name}={raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


@dataclass(frozen=True)
class UniverseFilters:
    """Базовые фильтры вселенной инструментов (до расчёта сетапов)."""

    min_price: float = 5.0
    max_price: float = 5_000.0
    min_market_cap: float = 50_000_000.0
    min_avg_dollar_volume: float = 3_000_000.0
    min_avg_volume: float = 200_000.0
    min_history_bars: int = 200
    exclude_etf: bool = True
    max_symbols: int = 0  # 0 = без ограничения, >0 — усечение (для отладки)

    @classmethod
    def from_env(cls) -> "UniverseFilters":
        return cls(
            min_price=_env_float("MIN_PRICE", 5.0),
            max_price=_env_float("MAX_PRICE", 5_000.0),
            min_market_cap=_env_float("MIN_MARKET_CAP", 50_000_000.0),
            min_avg_dollar_volume=_env_float("MIN_AVG_DOLLAR_VOLUME", 3_000_000.0),
            min_avg_volume=_env_float("MIN_AVG_VOLUME", 200_000.0),
            min_history_bars=_env_int("MIN_HISTORY_BARS", 200),
            exclude_etf=_env_bool("EXCLUDE_ETF", True),
            max_symbols=_env_int("MAX_SYMBOLS", 0),
        )


@dataclass(frozen=True)
class TradeSpec:
    """Параметры торгового плана — ядро технического задания."""

    min_target_pct: float = 0.20  # минимальная потенциальная прибыль
    max_risk_pct: float = 0.02  # максимальный риск на стоп-лосс
    min_hold_days: int = 10  # ~2 недели торговых дней
    max_hold_days: int = 15  # ~3 недели торговых дней
    entry_buffer_pct: float = 0.001  # буфер над триггером на проскальзывание

    # Совместимость цели и стопа с волатильностью бумаги.
    # 1) стоп 2% не должен быть внутри дневного шума: max_risk_pct >= k * ATR%
    min_stop_atr_ratio: float = 0.75
    # 2) цель +20% должна быть достижима за горизонт: target <= ATR% * дни * k
    max_target_atr_ratio: float = 1.0

    # Эмпирическая проверка сетапа на истории самой бумаги.
    min_backtest_signals: int = 5
    min_backtest_hit_rate: float = 0.10
    min_backtest_expectancy_r: float = 0.0

    @property
    def min_rr(self) -> float:
        """Требуемое соотношение прибыль/риск (по ТЗ = 10:1)."""
        return self.min_target_pct / self.max_risk_pct

    @classmethod
    def from_env(cls) -> "TradeSpec":
        return cls(
            min_target_pct=_env_float("MIN_TARGET_PCT", 0.20),
            max_risk_pct=_env_float("MAX_RISK_PCT", 0.02),
            min_hold_days=_env_int("MIN_HOLD_DAYS", 10),
            max_hold_days=_env_int("MAX_HOLD_DAYS", 15),
            entry_buffer_pct=_env_float("ENTRY_BUFFER_PCT", 0.001),
            min_stop_atr_ratio=_env_float("MIN_STOP_ATR_RATIO", 0.75),
            max_target_atr_ratio=_env_float("MAX_TARGET_ATR_RATIO", 1.0),
            min_backtest_signals=_env_int("MIN_BACKTEST_SIGNALS", 5),
            min_backtest_hit_rate=_env_float("MIN_BACKTEST_HIT_RATE", 0.10),
            min_backtest_expectancy_r=_env_float("MIN_BACKTEST_EXPECTANCY_R", 0.0),
        )


@dataclass(frozen=True)
class TelegramConfig:
    token: str = ""
    chat_id: str = ""
    parse_mode: str = "HTML"
    timeout: float = 30.0
    max_retries: int = 4
    disable_web_page_preview: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            token=_env_str("TELEGRAM_BOT_TOKEN", ""),
            chat_id=_env_str("TELEGRAM_CHAT_ID", ""),
            parse_mode=_env_str("TELEGRAM_PARSE_MODE", "HTML"),
            timeout=_env_float("TELEGRAM_TIMEOUT", 30.0),
            max_retries=_env_int("TELEGRAM_MAX_RETRIES", 4),
            disable_web_page_preview=_env_bool("TELEGRAM_DISABLE_PREVIEW", True),
        )


@dataclass(frozen=True)
class DataConfig:
    history_period: str = "2y"
    batch_size: int = 120
    max_workers: int = 8
    request_retries: int = 3
    retry_backoff: float = 2.0
    benchmark: str = "SPY"
    cache_dir: Path = PROJECT_ROOT / "cache"
    use_cache: bool = True
    cache_ttl_minutes: int = 180

    @classmethod
    def from_env(cls) -> "DataConfig":
        return cls(
            history_period=_env_str("HISTORY_PERIOD", "2y"),
            batch_size=_env_int("BATCH_SIZE", 120),
            max_workers=_env_int("MAX_WORKERS", 8),
            request_retries=_env_int("REQUEST_RETRIES", 3),
            retry_backoff=_env_float("RETRY_BACKOFF", 2.0),
            benchmark=_env_str("BENCHMARK", "SPY"),
            cache_dir=Path(_env_str("CACHE_DIR", str(PROJECT_ROOT / "cache"))),
            use_cache=_env_bool("USE_CACHE", True),
            cache_ttl_minutes=_env_int("CACHE_TTL_MINUTES", 180),
        )


@dataclass(frozen=True)
class Settings:
    filters: UniverseFilters = field(default_factory=UniverseFilters)
    trade: TradeSpec = field(default_factory=TradeSpec)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    data: DataConfig = field(default_factory=DataConfig)

    timezone: str = DEFAULT_TIMEZONE
    scan_times: tuple[str, ...] = ("09:00", "21:00")
    max_results: int = 10
    min_score: float = 0.0
    include_partial_bar: bool = True
    send_empty_report: bool = True
    state_dir: Path = PROJECT_ROOT / "state"
    reports_dir: Path = PROJECT_ROOT / "reports"
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = None) -> "Settings":
        """Читает .env (если есть) и собирает настройки из окружения."""
        if env_file is not None:
            load_dotenv(env_file, override=False)
        else:
            default_env = PROJECT_ROOT / ".env"
            if default_env.exists():
                load_dotenv(default_env, override=False)

        times = tuple(
            part.strip()
            for part in _env_str("SCAN_TIMES", DEFAULT_SCAN_TIMES).split(",")
            if part.strip()
        )
        return cls(
            filters=UniverseFilters.from_env(),
            trade=TradeSpec.from_env(),
            telegram=TelegramConfig.from_env(),
            data=DataConfig.from_env(),
            timezone=_env_str("TIMEZONE", DEFAULT_TIMEZONE),
            scan_times=times or ("09:00", "21:00"),
            max_results=_env_int("MAX_RESULTS", 10),
            min_score=_env_float("MIN_SCORE", 0.0),
            include_partial_bar=_env_bool("INCLUDE_PARTIAL_BAR", True),
            send_empty_report=_env_bool("SEND_EMPTY_REPORT", True),
            state_dir=Path(_env_str("STATE_DIR", str(PROJECT_ROOT / "state"))),
            reports_dir=Path(_env_str("REPORTS_DIR", str(PROJECT_ROOT / "reports"))),
            log_level=_env_str("LOG_LEVEL", "INFO"),
        )

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.reports_dir, self.data.cache_dir):
            Path(path).mkdir(parents=True, exist_ok=True)


def parse_hhmm(value: str) -> tuple[int, int]:
    """'09:00' -> (9, 0). Бросает ValueError на некорректном значении."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Ожидался формат ЧЧ:ММ, получено {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Некорректное время {value!r}")
    return hour, minute
