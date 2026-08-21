"""Модели данных: сигнал, результат бэктеста, кандидат, итог сканирования."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class SetupType:
    code: str
    title: str


BREAKOUT = SetupType("BO", "Пробой сжатой консолидации")
PULLBACK = SetupType("PB", "Откат к скользящей в восходящем тренде")
MOMENTUM = SetupType("MOM", "Продолжение импульса у годовых максимумов")

SETUPS: dict[str, SetupType] = {s.code: s for s in (BREAKOUT, PULLBACK, MOMENTUM)}


@dataclass
class BacktestStats:
    """Статистика того же правила входа на истории конкретной бумаги."""

    signals: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    avg_bars_held: float = 0.0
    expectancy_r: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.wins / self.signals if self.signals else 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hit_rate"] = round(self.hit_rate, 4)
        return data


@dataclass
class TradeIdea:
    """Готовая торговая идея по одной бумаге."""

    symbol: str
    name: str
    setup_code: str
    as_of: date

    last_close: float
    entry: float
    stop: float
    target: float

    risk_pct: float
    reward_pct: float
    rr: float

    atr_pct: float
    adx: float
    rsi: float
    rs_63d: float
    dist_52w_high: float
    volume_ratio: float
    avg_dollar_volume: float
    market_cap: float

    min_hold_days: int
    max_hold_days: int
    deadline: date

    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    backtest: BacktestStats = field(default_factory=BacktestStats)
    sector: str = ""
    exchange: str = ""

    @property
    def setup_title(self) -> str:
        setup = SETUPS.get(self.setup_code)
        return setup.title if setup else self.setup_code

    @property
    def shares_per_1pct_risk(self) -> float:
        """Сколько акций на каждый $1000 капитала при риске 1% на сделку."""
        risk_per_share = self.entry - self.stop
        if risk_per_share <= 0:
            return 0.0
        return 10.0 / risk_per_share  # 1% от $1000 = $10

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["deadline"] = self.deadline.isoformat()
        data["setup_title"] = self.setup_title
        data["backtest"] = self.backtest.to_dict()
        return data


@dataclass
class FunnelStats:
    """Сколько бумаг отсеялось на каждом шаге — для прозрачности отчёта."""

    universe: int = 0
    with_data: int = 0
    passed_price: int = 0
    passed_liquidity: int = 0
    passed_market_cap: int = 0
    passed_setup: int = 0
    passed_risk_reward: int = 0
    passed_feasibility: int = 0
    passed_backtest: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScanResult:
    started_at: datetime
    finished_at: datetime
    as_of: date | None
    ideas: list[TradeIdea] = field(default_factory=list)
    funnel: FunnelStats = field(default_factory=FunnelStats)
    errors: list[str] = field(default_factory=list)
    partial_session: bool = False

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "partial_session": self.partial_session,
            "funnel": self.funnel.to_dict(),
            "errors": self.errors,
            "ideas": [idea.to_dict() for idea in self.ideas],
        }
