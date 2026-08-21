"""Оркестрация сканирования: вселенная -> данные -> фильтры -> сетапы -> идеи."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import pandas as pd

from .backtest import backtest_symbol, passes_history_filter
from .config import Settings
from .indicators import add_indicators
from .market_clock import add_trading_days, is_session_open
from .market_data import MarketData, Quote
from .models import FunnelStats, ScanResult, TradeIdea
from .strategy import (
    build_plan,
    check_feasibility,
    check_risk_reward,
    detect_setup,
    prepare,
    score_idea,
)
from .universe import SymbolInfo, fetch_universe

log = logging.getLogger(__name__)

# Насколько последний бар бумаги может отставать от самой свежей даты рынка.
# Больший разрыв означает остановку торгов, делистинг или дыру в данных.
MAX_STALE_DAYS = 5


class Scanner:
    def __init__(
        self,
        settings: Settings,
        market_data: MarketData | None = None,
        universe_provider=None,
    ) -> None:
        self.settings = settings
        self.market_data = market_data or MarketData(
            period=settings.data.history_period,
            batch_size=settings.data.batch_size,
            retries=settings.data.request_retries,
            retry_backoff=settings.data.retry_backoff,
            cache_dir=settings.data.cache_dir,
            cache_ttl_minutes=settings.data.cache_ttl_minutes,
            use_cache=settings.data.use_cache,
        )
        self._universe_provider = universe_provider

    # ------------------------------------------------------------------ API
    def run(self, now: datetime | None = None) -> ScanResult:
        started = now or datetime.now(timezone.utc)
        funnel = FunnelStats()
        errors: list[str] = []

        symbols = self._load_universe(errors)
        funnel.universe = len(symbols)
        by_symbol = {info.yahoo_symbol: info for info in symbols}

        benchmark = self.settings.data.benchmark
        history = self.market_data.download_history([*by_symbol.keys(), benchmark])
        benchmark_close = self._benchmark_close(history, benchmark)
        history.pop(benchmark, None)
        funnel.with_data = len(history)
        if funnel.universe > 0 and funnel.with_data == 0:
            errors.append(
                "Не удалось загрузить котировки ни по одной бумаге — проверьте "
                "доступ к Yahoo Finance и сетевые ограничения"
            )

        partial = self.settings.include_partial_bar and is_session_open(started)
        history = {
            symbol: self._trim_partial_bar(frame, started, partial)
            for symbol, frame in history.items()
        }
        history = {s: f for s, f in history.items() if f is not None and not f.empty}

        prefiltered = self._prefilter(history, funnel)
        quotes = self._fetch_quotes(prefiltered, errors)
        candidates = self._apply_market_cap(prefiltered, quotes, funnel)

        candidates, as_of = self._drop_stale(candidates)

        ideas: list[TradeIdea] = []
        for symbol, frame in candidates.items():
            try:
                idea = self._evaluate(symbol, frame, by_symbol.get(symbol), quotes.get(symbol), funnel, benchmark_close)
            except Exception as exc:  # noqa: BLE001 - одна битая бумага не рушит скан
                log.debug("Ошибка анализа %s: %s", symbol, exc)
                continue
            if idea is not None:
                ideas.append(idea)

        ideas.sort(key=lambda item: item.score, reverse=True)
        if self.settings.min_score > 0:
            ideas = [i for i in ideas if i.score >= self.settings.min_score]
        if self.settings.max_results > 0:
            ideas = ideas[: self.settings.max_results]

        return ScanResult(
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            as_of=as_of.date() if as_of is not None else None,
            ideas=ideas,
            funnel=funnel,
            errors=errors,
            partial_session=partial,
        )

    # ------------------------------------------------------------- вселенная
    def _load_universe(self, errors: list[str]) -> list[SymbolInfo]:
        try:
            if self._universe_provider is not None:
                symbols = list(self._universe_provider())
            else:
                symbols = fetch_universe(
                    exclude_etf=self.settings.filters.exclude_etf,
                    cache_dir=self.settings.data.cache_dir,
                    timeout=30.0,
                    retries=self.settings.data.request_retries,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Не удалось получить список бумаг: {exc}")
            return []
        limit = self.settings.filters.max_symbols
        if limit > 0:
            symbols = symbols[:limit]
        return symbols

    @staticmethod
    def _benchmark_close(history: dict[str, pd.DataFrame], benchmark: str) -> pd.Series | None:
        frame = history.get(benchmark)
        return frame["Close"] if frame is not None and not frame.empty else None

    @staticmethod
    def _trim_partial_bar(
        frame: pd.DataFrame, now: datetime, keep_partial: bool
    ) -> pd.DataFrame:
        """Убирает незакрытый дневной бар, если решено считать только по закрытым."""
        if keep_partial or frame.empty:
            return frame
        from .market_clock import NY

        today_ny = now.astimezone(NY).date()
        if frame.index[-1].date() >= today_ny:
            return frame.iloc[:-1]
        return frame

    # -------------------------------------------------------------- фильтры
    def _prefilter(
        self, history: dict[str, pd.DataFrame], funnel: FunnelStats
    ) -> dict[str, pd.DataFrame]:
        filters = self.settings.filters
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in history.items():
            if len(frame) < filters.min_history_bars:
                continue
            close = float(frame["Close"].iloc[-1])
            if not (filters.min_price <= close <= filters.max_price):
                continue
            funnel.passed_price += 1

            tail = frame.tail(20)
            avg_volume = float(tail["Volume"].mean())
            avg_dollar_volume = float((tail["Close"] * tail["Volume"]).mean())
            if avg_volume < filters.min_avg_volume:
                continue
            if avg_dollar_volume < filters.min_avg_dollar_volume:
                continue
            funnel.passed_liquidity += 1
            out[symbol] = frame
        return out

    def _fetch_quotes(
        self, candidates: dict[str, pd.DataFrame], errors: list[str]
    ) -> dict[str, Quote]:
        if not candidates:
            return {}
        try:
            return self.market_data.fetch_quotes(sorted(candidates.keys()))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Не удалось получить капитализацию: {exc}")
            return {}

    def _apply_market_cap(
        self,
        candidates: dict[str, pd.DataFrame],
        quotes: dict[str, Quote],
        funnel: FunnelStats,
    ) -> dict[str, pd.DataFrame]:
        min_cap = self.settings.filters.min_market_cap
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in candidates.items():
            quote = quotes.get(symbol)
            if quote is None or quote.market_cap <= 0:
                # Без подтверждённой капитализации бумагу не берём: требование ТЗ
                # (>= $50 млн) должно проверяться, а не предполагаться.
                continue
            if quote.market_cap < min_cap:
                continue
            funnel.passed_market_cap += 1
            out[symbol] = frame
        return out

    @staticmethod
    def _drop_stale(
        candidates: dict[str, pd.DataFrame],
    ) -> tuple[dict[str, pd.DataFrame], pd.Timestamp | None]:
        """Отсекает бумаги, отставшие от рынка (остановка торгов, делистинг)."""
        if not candidates:
            return candidates, None
        latest = max(frame.index[-1] for frame in candidates.values())
        cutoff = latest - pd.Timedelta(days=MAX_STALE_DAYS)
        fresh = {s: f for s, f in candidates.items() if f.index[-1] >= cutoff}
        skipped = len(candidates) - len(fresh)
        if skipped:
            log.info("Пропущено %d бумаг с устаревшими данными", skipped)
        return fresh, latest

    # ---------------------------------------------------------------- анализ
    def _evaluate(
        self,
        symbol: str,
        frame: pd.DataFrame,
        info: SymbolInfo | None,
        quote: Quote | None,
        funnel: FunnelStats,
        benchmark_close: pd.Series | None,
    ) -> TradeIdea | None:
        spec = self.settings.trade
        enriched = add_indicators(frame, benchmark_close)
        data = prepare(enriched)
        last = len(data) - 1

        signal = detect_setup(data, last)
        if signal is None:
            return None
        funnel.passed_setup += 1

        plan = build_plan(signal, spec)
        rr_check = check_risk_reward(plan, spec)
        if not rr_check.ok:
            return None
        funnel.passed_risk_reward += 1

        atr_value = data.get("atr_pct", last)
        recent_low = float(min(data.array("Low")[max(0, last - 1) : last + 1]))
        feasibility = check_feasibility(plan, atr_value, recent_low, spec)
        if not feasibility.ok:
            return None
        funnel.passed_feasibility += 1

        stats = backtest_symbol(data, spec, skip_last=1)
        history_ok, history_note = passes_history_filter(stats, spec)
        if not history_ok:
            return None
        funnel.passed_backtest += 1

        score, parts = score_idea(
            adx_value=data.get("adx14", last),
            rs_63d=data.get("rs_63d", last),
            range20=data.get("range20_pct", last),
            atr_pct_value=atr_value,
            dist_high=data.get("dist_52w_high", last),
            avg_dollar_volume=data.get("dollar_volume20", last),
            rr=plan.rr,
            hit_rate=stats.hit_rate,
            expectancy_r=stats.expectancy_r,
            spec=spec,
        )

        reasons = list(signal.reasons)
        if history_note:
            reasons.append(history_note)

        as_of_date = enriched.index[-1].date()
        return TradeIdea(
            symbol=symbol,
            name=info.name if info else symbol,
            setup_code=signal.setup_code,
            as_of=as_of_date,
            last_close=float(enriched["Close"].iloc[-1]),
            entry=round(plan.entry, 2),
            stop=round(plan.stop, 2),
            target=round(plan.target, 2),
            risk_pct=plan.risk_pct,
            reward_pct=plan.reward_pct,
            rr=plan.rr,
            atr_pct=_nan_to_zero(atr_value),
            adx=_nan_to_zero(data.get("adx14", last)),
            rsi=_nan_to_zero(data.get("rsi14", last)),
            rs_63d=_nan_to_zero(data.get("rs_63d", last)),
            dist_52w_high=_nan_to_zero(data.get("dist_52w_high", last)),
            volume_ratio=_nan_to_zero(data.get("volume_ratio", last)),
            avg_dollar_volume=_nan_to_zero(data.get("dollar_volume20", last)),
            market_cap=quote.market_cap if quote else 0.0,
            min_hold_days=spec.min_hold_days,
            max_hold_days=spec.max_hold_days,
            deadline=add_trading_days(as_of_date, spec.max_hold_days),
            score=score,
            score_parts=parts,
            reasons=reasons,
            backtest=stats,
            exchange=info.exchange if info else "",
        )


def _nan_to_zero(value: float) -> float:
    if value is None or math.isnan(value) or math.isinf(value):
        return 0.0
    return float(value)
