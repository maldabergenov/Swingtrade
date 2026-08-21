from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swingscan.config import Settings, TradeSpec, UniverseFilters  # noqa: E402


_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_PARSE_MODE",
    "TELEGRAM_TIMEOUT", "TELEGRAM_MAX_RETRIES", "TELEGRAM_DISABLE_PREVIEW",
    "MIN_PRICE", "MAX_PRICE", "MIN_MARKET_CAP", "MIN_AVG_DOLLAR_VOLUME",
    "MIN_AVG_VOLUME", "MIN_HISTORY_BARS", "EXCLUDE_ETF", "MAX_SYMBOLS",
    "MIN_TARGET_PCT", "MAX_RISK_PCT", "MIN_HOLD_DAYS", "MAX_HOLD_DAYS",
    "ENTRY_BUFFER_PCT", "MIN_STOP_ATR_RATIO", "MAX_TARGET_ATR_RATIO",
    "MIN_BACKTEST_SIGNALS", "MIN_BACKTEST_HIT_RATE", "MIN_BACKTEST_EXPECTANCY_R",
    "TIMEZONE", "SCAN_TIMES", "MAX_RESULTS", "MIN_SCORE", "SEND_EMPTY_REPORT",
    "INCLUDE_PARTIAL_BAR", "STATE_DIR", "REPORTS_DIR", "CACHE_DIR", "LOG_LEVEL",
    "HISTORY_PERIOD", "BATCH_SIZE", "MAX_WORKERS", "REQUEST_RETRIES",
    "RETRY_BACKOFF", "BENCHMARK", "USE_CACHE", "CACHE_TTL_MINUTES",
)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """Тесты не должны зависеть от локального .env и окружения разработчика."""
    monkeypatch.setattr("swingscan.config.load_dotenv", lambda *_a, **_k: False)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def trade_spec() -> TradeSpec:
    return TradeSpec()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Настройки по умолчанию с изолированными каталогами."""
    base = Settings()
    return base.with_overrides(
        state_dir=tmp_path / "state",
        reports_dir=tmp_path / "reports",
        data=base.data.__class__(cache_dir=tmp_path / "cache", use_cache=False),
        filters=UniverseFilters(),
    )
