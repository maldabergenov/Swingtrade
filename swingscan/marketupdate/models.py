"""Модели данных маркет-апдейта.

Каждый из трёх блоков отчёта представлен ``Section``: он либо собран
(``ok=True``), либо помечен причиной сбоя. Отчёт формируется из любого
набора секций — упавший источник не мешает отправке остальных.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class QuoteChange:
    """Закрытие инструмента и изменение к предыдущей сессии."""

    symbol: str
    title: str
    close: float
    change_pct: float
    previous_close: float = 0.0
    session_date: str = ""

    @property
    def change_abs(self) -> float:
        return self.close - self.previous_close

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "title": self.title,
            "close": round(self.close, 4),
            "change_pct": round(self.change_pct, 4),
            "previous_close": round(self.previous_close, 4),
            "session_date": self.session_date,
        }


@dataclass(frozen=True)
class Headline:
    """Заголовок новости из RSS или API."""

    title: str
    url: str
    source: str
    published_at: datetime | None = None
    summary: str = ""  # анонс из ленты (не публикуется дословно)
    score: float = 0.0
    # Заполняется на этапе пересказа: одна строка на русском.
    retelling: str = ""
    tag: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "score": round(self.score, 3),
            "retelling": self.retelling,
            "tag": self.tag,
        }


@dataclass(frozen=True)
class SocialPost:
    """Пост или тред из X."""

    author: str
    text: str
    url: str
    published_at: datetime | None = None
    likes: int = 0
    retelling: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "author": self.author,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "likes": self.likes,
            "retelling": self.retelling,
        }


@dataclass
class Section:
    """Блок отчёта: собранный или помеченный причиной недоступности."""

    key: str
    title: str
    ok: bool = True
    reason: str = ""  # почему блок пуст — попадает в текст отчёта
    hint: str = ""  # что сделать, чтобы блок заработал (например, добавить ключ)

    @classmethod
    def failed(cls, key: str, title: str, reason: str, hint: str = "") -> "Section":
        return cls(key=key, title=title, ok=False, reason=reason, hint=hint)


@dataclass
class MarketSection(Section):
    indices: list[QuoteChange] = field(default_factory=list)
    sectors: list[QuoteChange] = field(default_factory=list)
    session_date: str = ""
    provider: str = ""
    context: str = ""  # 1–2 предложения, что двигало рынок

    @property
    def gainers(self) -> list[QuoteChange]:
        return sorted(self.sectors, key=lambda q: q.change_pct, reverse=True)[:3]

    @property
    def losers(self) -> list[QuoteChange]:
        return sorted(self.sectors, key=lambda q: q.change_pct)[:3]


@dataclass
class NewsSection(Section):
    headlines: list[Headline] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)


@dataclass
class SocialSection(Section):
    posts: list[SocialPost] = field(default_factory=list)
    provider: str = "none"


@dataclass
class MarketUpdate:
    """Готовый отчёт целиком."""

    generated_at: datetime
    market: MarketSection
    news: NewsSection
    social: SocialSection
    llm_used: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def sections(self) -> tuple[Section, ...]:
        return (self.market, self.news, self.social)

    @property
    def all_failed(self) -> bool:
        return all(not section.ok for section in self.sections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "llm_used": self.llm_used,
            "warnings": list(self.warnings),
            "market": {
                "ok": self.market.ok,
                "reason": self.market.reason,
                "provider": self.market.provider,
                "session_date": self.market.session_date,
                "context": self.market.context,
                "indices": [q.to_dict() for q in self.market.indices],
                "sectors": [q.to_dict() for q in self.market.sectors],
            },
            "news": {
                "ok": self.news.ok,
                "reason": self.news.reason,
                "sources_used": list(self.news.sources_used),
                "sources_failed": list(self.news.sources_failed),
                "headlines": [h.to_dict() for h in self.news.headlines],
            },
            "social": {
                "ok": self.social.ok,
                "reason": self.social.reason,
                "hint": self.social.hint,
                "provider": self.social.provider,
                "posts": [p.to_dict() for p in self.social.posts],
            },
        }
