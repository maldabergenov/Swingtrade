"""Конфигурация маркет-апдейта: TOML-файл + секреты из окружения.

Разделение намеренное:

* всё, что вы захотите править руками (индексы, секторы, RSS-ленты, список
  аккаунтов X), лежит в ``config/market_update.toml`` и коммитится в репозиторий;
* всё, что нельзя коммитить (токены), читается из окружения — тем же способом,
  что и в сканере: GitHub Secrets в Actions, ``.env`` локально.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, TelegramConfig, _env_bool, _env_float, _env_int, _env_str

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "market_update.toml"

# Провайдеры X, для которых у нас есть адаптер. "none" — блок отключён.
X_PROVIDERS = ("none", "x_api", "twitterapi_io", "apidance", "rss")


class MarketUpdateConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstrumentSpec:
    """Инструмент отчёта: тикер, запасной тикер и человеческое название."""

    symbol: str
    title: str
    proxy: str = ""

    def symbol_for(self, provider: str) -> str:
        """У Alpha Vantage нет индексов — там используется ETF-прокси."""
        if provider == "alphavantage" and self.proxy:
            return self.proxy
        return self.symbol


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str


@dataclass(frozen=True)
class MarketUpdateConfig:
    # --- общее ---
    timezone: str = "Asia/Almaty"
    lookback_hours: int = 16
    max_headlines: int = 7
    max_tweets: int = 6
    http_timeout: float = 20.0
    user_agent: str = "swingscan-market-update/1.0"

    # --- блок 1 ---
    market_providers: tuple[str, ...] = ("yfinance", "alphavantage")
    indices: tuple[InstrumentSpec, ...] = ()
    sectors: tuple[InstrumentSpec, ...] = ()

    # --- блок 2 ---
    feeds: tuple[FeedSpec, ...] = ()
    priority_high: tuple[str, ...] = ()
    priority_medium: tuple[str, ...] = ()
    news_use_alphavantage: bool = True

    # --- блок 3 ---
    x_provider: str = "none"
    x_rss_base_url: str = ""
    x_accounts: tuple[str, ...] = ()
    x_min_likes: int = 0

    # --- пересказ ---
    llm_enabled: bool = True
    llm_model: str = "claude-haiku-4-5"
    llm_max_tokens: int = 4000

    # --- секреты (только из окружения) ---
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    alphavantage_key: str = ""
    anthropic_key: str = ""
    x_bearer_token: str = ""
    x_api_key: str = ""

    # --- пути ---
    state_dir: Path = PROJECT_ROOT / "state"
    reports_dir: Path = PROJECT_ROOT / "reports"
    log_level: str = "INFO"

    # ------------------------------------------------------------- свойства
    @property
    def llm_available(self) -> bool:
        return self.llm_enabled and bool(self.anthropic_key)

    @property
    def x_credential(self) -> str:
        """Токен, нужный выбранному провайдеру X (пустой — значит нет доступа)."""
        if self.x_provider == "x_api":
            return self.x_bearer_token
        if self.x_provider in ("twitterapi_io", "apidance"):
            return self.x_api_key
        if self.x_provider == "rss":
            return self.x_rss_base_url
        return ""

    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "*/*"}

    # -------------------------------------------------------------- загрузка
    @classmethod
    def load(
        cls,
        config_path: str | os.PathLike[str] | None = None,
        env_file: str | os.PathLike[str] | None = None,
    ) -> "MarketUpdateConfig":
        """Читает .env (как сканер), затем TOML, затем секреты из окружения."""
        from ..config import load_dotenv

        if env_file is not None:
            load_dotenv(env_file, override=False)
        elif (PROJECT_ROOT / ".env").exists():
            load_dotenv(PROJECT_ROOT / ".env", override=False)

        path = Path(config_path or _env_str("MARKET_UPDATE_CONFIG", str(DEFAULT_CONFIG_PATH)))
        raw = _read_toml(path)

        general = _table(raw, "general")
        market = _table(raw, "market")
        news = _table(raw, "news")
        x_block = _table(raw, "x")
        llm = _table(raw, "llm")
        priority = _table(news, "priority")

        provider = str(x_block.get("provider", "none")).strip().lower() or "none"
        if provider not in X_PROVIDERS:
            raise MarketUpdateConfigError(
                f"Неизвестный provider={provider!r} в [x]. Допустимые: {', '.join(X_PROVIDERS)}"
            )

        return cls(
            timezone=_env_str("TIMEZONE", str(general.get("timezone", "Asia/Almaty"))),
            lookback_hours=_env_int(
                "MARKET_UPDATE_LOOKBACK_HOURS", int(general.get("lookback_hours", 16))
            ),
            max_headlines=_env_int(
                "MARKET_UPDATE_MAX_HEADLINES", int(general.get("max_headlines", 7))
            ),
            max_tweets=_env_int("MARKET_UPDATE_MAX_TWEETS", int(general.get("max_tweets", 6))),
            http_timeout=_env_float(
                "MARKET_UPDATE_HTTP_TIMEOUT", float(general.get("http_timeout", 20.0))
            ),
            user_agent=str(general.get("user_agent", "swingscan-market-update/1.0")),
            market_providers=_str_tuple(market.get("providers"), ("yfinance", "alphavantage")),
            indices=_instruments(market.get("indices")),
            sectors=_instruments(market.get("sectors")),
            feeds=_feeds(news.get("feeds")),
            priority_high=_str_tuple(priority.get("high"), ()),
            priority_medium=_str_tuple(priority.get("medium"), ()),
            news_use_alphavantage=bool(news.get("use_alphavantage", True)),
            x_provider=_env_str("X_PROVIDER", provider),
            x_rss_base_url=_env_str("X_RSS_BASE_URL", str(x_block.get("rss_base_url", ""))),
            x_accounts=_accounts(x_block.get("accounts")),
            x_min_likes=int(x_block.get("min_likes", 0)),
            llm_enabled=_env_bool("MARKET_UPDATE_LLM", bool(llm.get("enabled", True))),
            llm_model=_env_str("MARKET_UPDATE_LLM_MODEL", str(llm.get("model", "claude-haiku-4-5"))),
            llm_max_tokens=int(llm.get("max_tokens", 4000)),
            telegram=TelegramConfig.from_env(),
            alphavantage_key=_env_str("ALPHAVANTAGE_API_KEY", ""),
            anthropic_key=_env_str("ANTHROPIC_API_KEY", ""),
            x_bearer_token=_env_str("X_BEARER_TOKEN", ""),
            x_api_key=_env_str("X_API_KEY", ""),
            state_dir=Path(_env_str("STATE_DIR", str(PROJECT_ROOT / "state"))),
            reports_dir=Path(_env_str("REPORTS_DIR", str(PROJECT_ROOT / "reports"))),
            log_level=_env_str("LOG_LEVEL", "INFO"),
        )

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.reports_dir):
            Path(path).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- разбор TOML
def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MarketUpdateConfigError(
            f"Не найден конфиг маркет-апдейта: {path}. "
            "Проверьте путь или переменную MARKET_UPDATE_CONFIG."
        )
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MarketUpdateConfigError(f"Некорректный TOML в {path}: {exc}") from exc


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _str_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    items = tuple(str(item).strip() for item in value if str(item).strip())
    return items or default


def _instruments(value: Any) -> tuple[InstrumentSpec, ...]:
    if not isinstance(value, list):
        return ()
    specs: list[InstrumentSpec] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        specs.append(
            InstrumentSpec(
                symbol=symbol,
                title=str(item.get("title", symbol)).strip() or symbol,
                proxy=str(item.get("proxy", "")).strip(),
            )
        )
    return tuple(specs)


def _feeds(value: Any) -> tuple[FeedSpec, ...]:
    if not isinstance(value, list):
        return ()
    feeds: list[FeedSpec] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        feeds.append(FeedSpec(name=str(item.get("name", url)).strip() or url, url=url))
    return tuple(feeds)


def _accounts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    seen: list[str] = []
    for item in value:
        handle = str(item).strip().lstrip("@")
        if handle and handle.lower() not in {h.lower() for h in seen}:
            seen.append(handle)
    return tuple(seen)
