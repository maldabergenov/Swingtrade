"""Клиент Telegram Bot API: отправка отчётов и определение chat_id."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .config import TelegramConfig
from .report import TELEGRAM_LIMIT, split_message

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        state_dir: Path | None = None,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        api_base: str = API_BASE,
    ) -> None:
        if not config.token:
            raise TelegramError(
                "Не задан TELEGRAM_BOT_TOKEN. Укажите его в .env или в окружении."
            )
        self.config = config
        self.state_dir = Path(state_dir) if state_dir else None
        self.session = session or requests.Session()
        self._sleep = sleep
        self._api_base = api_base.rstrip("/")
        self._chat_id: str | None = config.chat_id or None

    # ---------------------------------------------------------------- низкий
    def _url(self, method: str) -> str:
        return f"{self._api_base}/bot{self.config.token}/{method}"

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Вызов метода API с повторами на 429 и 5xx."""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                response = self.session.post(
                    self._url(method), json=payload or {}, timeout=self.config.timeout
                )
            except Exception as exc:  # noqa: BLE001 - сетевые сбои повторяем
                last_error = exc
                wait = 2.0 * (2**attempt)
                log.warning("Telegram %s: сеть недоступна (%s), жду %.0fс", method, exc, wait)
                self._sleep(wait)
                continue

            status = getattr(response, "status_code", 0)
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                body = {}

            if status == 200 and body.get("ok"):
                return body.get("result", {})

            if status == 429:
                retry_after = float(
                    body.get("parameters", {}).get("retry_after", 2.0 * (2**attempt))
                )
                log.warning("Telegram %s: лимит запросов, пауза %.0fс", method, retry_after)
                self._sleep(retry_after + 0.5)
                continue

            description = body.get("description", f"HTTP {status}")
            if 500 <= status < 600:
                wait = 2.0 * (2**attempt)
                log.warning("Telegram %s: %s, жду %.0fс", method, description, wait)
                self._sleep(wait)
                last_error = TelegramError(description)
                continue

            raise TelegramError(f"Telegram API {method}: {description}")

        raise TelegramError(f"Telegram API {method}: не удалось выполнить запрос ({last_error})")

    # --------------------------------------------------------------- высокий
    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def resolve_chat_id(self) -> str:
        """Ищет chat_id: конфиг -> сохранённое состояние -> getUpdates."""
        if self._chat_id:
            return self._chat_id

        stored = self._load_chat_id()
        if stored:
            self._chat_id = stored
            return stored

        updates = self._call("getUpdates", {"limit": 50, "timeout": 0})
        chat_id = _extract_chat_id(updates)
        if not chat_id:
            raise TelegramError(
                "Не удалось определить chat_id. Откройте бота в Telegram, отправьте "
                "ему любое сообщение (например /start) и запустите команду ещё раз, "
                "либо задайте TELEGRAM_CHAT_ID вручную."
            )
        self._chat_id = chat_id
        self._save_chat_id(chat_id)
        return chat_id

    def send_message(self, text: str, chat_id: str | None = None) -> list[dict[str, Any]]:
        """Отправляет текст, автоматически разбивая его на части по 4096 символов."""
        target = chat_id or self.resolve_chat_id()
        results = []
        chunks = split_message(text, TELEGRAM_LIMIT)
        for index, chunk in enumerate(chunks):
            payload = {
                "chat_id": target,
                "text": chunk,
                "parse_mode": self.config.parse_mode,
                "disable_web_page_preview": self.config.disable_web_page_preview,
            }
            results.append(self._call("sendMessage", payload))
            if index + 1 < len(chunks):
                self._sleep(0.6)  # мягкий темп, чтобы не ловить 429
        return results

    # ----------------------------------------------------------------- кэш
    def _state_file(self) -> Path | None:
        return self.state_dir / "telegram_chat.json" if self.state_dir else None

    def _load_chat_id(self) -> str | None:
        path = self._state_file()
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        chat_id = data.get("chat_id")
        return str(chat_id) if chat_id else None

    def _save_chat_id(self, chat_id: str) -> None:
        path = self._state_file()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")


def _extract_chat_id(updates: Any) -> str | None:
    """Берёт chat_id из последнего входящего сообщения."""
    if not isinstance(updates, list):
        return None
    for update in reversed(updates):
        for key in ("message", "channel_post", "edited_message", "my_chat_member"):
            container = update.get(key) if isinstance(update, dict) else None
            if isinstance(container, dict):
                chat = container.get("chat")
                if isinstance(chat, dict) and chat.get("id") is not None:
                    return str(chat["id"])
    return None
