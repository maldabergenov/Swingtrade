"""Пересказ заголовков и постов на русском через Claude API.

Один вызов на весь отчёт: модель получает уже отобранные факты (заголовки,
анонсы из лент, посты, цифры по индексам и секторам) и возвращает JSON с
короткими формулировками своими словами — дословный текст статей не
копируется и в Telegram не уходит.

Блок полностью опциональный. Нет ``ANTHROPIC_API_KEY``, не установлен пакет
``anthropic``, вызов упал или вернул мусор — отчёт уходит с заголовками на
языке оригинала и тематической меткой из ``news._tag``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any

from .config import MarketUpdateConfig
from .models import MarketUpdate

log = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = """\
Ты — редактор утренней сводки для частного трейдера. Пишешь по-русски, сухо и \
по делу, без вводных оборотов и без оценочных прилагательных.

Правила:
1. Каждый заголовок пересказывай СВОИМИ СЛОВАМИ одной строкой — суть события и \
почему это важно для широкого рынка. Не переводи дословно и не копируй текст.
2. Длина строки — 90–160 символов. Без кавычек по краям, без эмодзи, без markdown.
3. Если из данных не понятно, что произошло, напиши нейтральный пересказ по \
имеющимся фактам — ничего не выдумывай и не добавляй чисел, которых нет во входных данных.
4. `context` — 1–2 предложения о том, что двигало рынок в прошлую сессию, \
опираясь ТОЛЬКО на переданные проценты по индексам и секторам и на заголовки.

Отвечай ТОЛЬКО валидным JSON без markdown-обёртки, по схеме:
{"context": "строка", "headlines": [{"id": 0, "text": "строка"}], \
"posts": [{"id": 0, "text": "строка"}]}
Массив headlines должен содержать по одному элементу на каждый входной заголовок \
с тем же id. То же для posts.\
"""


def enrich(
    update: MarketUpdate, config: MarketUpdateConfig, *, client: Any | None = None
) -> MarketUpdate:
    """Дополняет отчёт русскими пересказами. Возвращает отчёт в любом случае."""
    if client is None and not config.llm_available:
        reason = (
            "пересказ отключён в конфиге"
            if not config.llm_enabled
            else "нет ANTHROPIC_API_KEY — заголовки на языке оригинала"
        )
        log.info("Пересказ пропущен: %s", reason)
        update.warnings.append(reason)
        return update

    headlines = update.news.headlines if update.news.ok else []
    posts = update.social.posts if update.social.ok else []
    if not headlines and not posts:
        log.info("Пересказ пропущен: нечего пересказывать")
        return update

    try:
        client = client or _build_client(config)
        payload = _request(client, config, update, headlines, posts)
    except Exception as exc:  # noqa: BLE001 - LLM не должен ронять отчёт
        log.warning("Пересказ недоступен (%s) — оставляю оригинальные заголовки", exc)
        update.warnings.append(f"пересказ на русском недоступен: {exc}")
        return update

    _apply(update, payload, headlines, posts)
    update.llm_used = True
    log.info("Пересказ применён: %d заголовков, %d постов", len(headlines), len(posts))
    return update


def _build_client(config: MarketUpdateConfig) -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise RuntimeError("пакет anthropic не установлен") from exc
    return anthropic.Anthropic(api_key=config.anthropic_key, timeout=config.http_timeout * 3)


def build_input(
    update: MarketUpdate, headlines: list[Any], posts: list[Any]
) -> dict[str, Any]:
    """Компактный JSON-вход для модели: только факты, ничего лишнего."""
    return {
        "market": {
            "session_date": update.market.session_date,
            "indices": [
                {"name": q.title, "close": round(q.close, 2), "change_pct": round(q.change_pct, 2)}
                for q in update.market.indices
            ],
            "top_sectors": [
                {"name": q.title, "change_pct": round(q.change_pct, 2)}
                for q in update.market.gainers
            ],
            "worst_sectors": [
                {"name": q.title, "change_pct": round(q.change_pct, 2)}
                for q in update.market.losers
            ],
        }
        if update.market.ok
        else {},
        "headlines": [
            {
                "id": index,
                "source": headline.source,
                "title": headline.title,
                "lead": headline.summary[:280],
            }
            for index, headline in enumerate(headlines)
        ],
        "posts": [
            {"id": index, "author": post.author, "text": post.text[:280]}
            for index, post in enumerate(posts)
        ],
    }


def _request(
    client: Any,
    config: MarketUpdateConfig,
    update: MarketUpdate,
    headlines: list[Any],
    posts: list[Any],
) -> dict[str, Any]:
    payload = build_input(update, headlines, posts)
    response = client.messages.create(
        model=config.llm_model,
        max_tokens=config.llm_max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Данные утренней сводки:\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2)
                ),
            }
        ],
    )
    if getattr(response, "stop_reason", "") == "refusal":
        raise RuntimeError("модель отклонила запрос")

    text = "".join(
        block.text
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
    ).strip()
    return _parse_json(text)


def _parse_json(text: str) -> dict[str, Any]:
    """Модель просили отдать голый JSON, но обёртку ```json``` терпим."""
    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
    match = _JSON_BLOCK_RE.search(candidate)
    if not match:
        raise ValueError("ответ без JSON")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("ожидался JSON-объект")
    return parsed


def _apply(
    update: MarketUpdate, payload: dict[str, Any], headlines: list[Any], posts: list[Any]
) -> None:
    context = str(payload.get("context", "")).strip()
    if context:
        update.market.context = context

    for index, text in _by_id(payload.get("headlines")).items():
        if 0 <= index < len(headlines):
            headlines[index] = replace(headlines[index], retelling=text)
    for index, text in _by_id(payload.get("posts")).items():
        if 0 <= index < len(posts):
            posts[index] = replace(posts[index], retelling=text)

    if update.news.ok:
        update.news.headlines = headlines
    if update.social.ok:
        update.social.posts = posts


def _by_id(items: Any) -> dict[int, str]:
    result: dict[int, str] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if text:
            result[index] = text
    return result
