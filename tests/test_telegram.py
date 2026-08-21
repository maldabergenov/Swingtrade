from __future__ import annotations

import json

import pytest

from swingscan.config import TelegramConfig
from swingscan.telegram import TelegramClient, TelegramError, _extract_chat_id


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[-1], json or {}))
        if not self.responses:
            raise AssertionError("Неожиданный дополнительный запрос к Telegram")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def ok(result):
    return FakeResponse(200, {"ok": True, "result": result})


def client(session, **config_kwargs):
    config = TelegramConfig(token="TESTTOKEN", **config_kwargs)
    return TelegramClient(config, session=session, sleep=lambda _s: None)


def test_missing_token_is_rejected_early():
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        TelegramClient(TelegramConfig(token=""))


def test_send_message_posts_html_payload():
    session = FakeSession([ok({"message_id": 1})])
    client(session, chat_id="42").send_message("<b>привет</b>")
    method, payload = session.calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True


def test_send_message_splits_long_text():
    session = FakeSession([ok({"message_id": i}) for i in range(20)])
    long_text = "\n".join(f"строка номер {i} " * 8 for i in range(200))
    results = client(session, chat_id="42").send_message(long_text)
    assert len(results) == len(session.calls) >= 2
    assert all(len(payload["text"]) <= 4096 for _, payload in session.calls)


def test_retries_on_rate_limit():
    session = FakeSession(
        [
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 1}}),
            ok({"message_id": 5}),
        ]
    )
    assert client(session, chat_id="42").send_message("текст")[0]["message_id"] == 5
    assert session.methods == ["sendMessage", "sendMessage"]


def test_retries_on_server_error_then_succeeds():
    session = FakeSession([FakeResponse(502, {"ok": False}), ok({"message_id": 9})])
    assert client(session, chat_id="42").send_message("текст")[0]["message_id"] == 9


def test_retries_on_network_error():
    session = FakeSession([ConnectionError("сеть недоступна"), ok({"message_id": 3})])
    assert client(session, chat_id="42").send_message("текст")[0]["message_id"] == 3


def test_gives_up_after_max_retries():
    session = FakeSession([FakeResponse(500, {"ok": False})] * 4)
    with pytest.raises(TelegramError):
        client(session, chat_id="42", max_retries=4).send_message("текст")


def test_client_error_is_not_retried():
    session = FakeSession([FakeResponse(400, {"ok": False, "description": "chat not found"})])
    with pytest.raises(TelegramError, match="chat not found"):
        client(session, chat_id="42").send_message("текст")
    assert len(session.calls) == 1


def test_resolve_chat_id_from_updates_and_persists(tmp_path):
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 111}}},
        {"update_id": 2, "message": {"chat": {"id": 222}}},
    ]
    session = FakeSession([ok(updates), ok({"message_id": 1})])
    config = TelegramConfig(token="T")
    telegram = TelegramClient(config, state_dir=tmp_path, session=session, sleep=lambda _s: None)

    assert telegram.resolve_chat_id() == "222"  # берём последний диалог
    stored = json.loads((tmp_path / "telegram_chat.json").read_text(encoding="utf-8"))
    assert stored["chat_id"] == "222"

    telegram.send_message("текст")
    assert session.methods == ["getUpdates", "sendMessage"]


def test_resolve_chat_id_reuses_saved_state(tmp_path):
    (tmp_path / "telegram_chat.json").write_text(json.dumps({"chat_id": "-100500"}))
    session = FakeSession([])
    telegram = TelegramClient(TelegramConfig(token="T"), state_dir=tmp_path, session=session)
    assert telegram.resolve_chat_id() == "-100500"
    assert session.calls == []


def test_resolve_chat_id_explains_how_to_fix_when_no_updates(tmp_path):
    session = FakeSession([ok([])])
    telegram = TelegramClient(TelegramConfig(token="T"), state_dir=tmp_path, session=session)
    with pytest.raises(TelegramError, match="/start"):
        telegram.resolve_chat_id()


def test_get_me_returns_bot_profile():
    session = FakeSession([ok({"id": 1, "username": "swing_bot"})])
    assert client(session).get_me()["username"] == "swing_bot"


@pytest.mark.parametrize(
    "updates,expected",
    [
        ([{"channel_post": {"chat": {"id": -100777}}}], "-100777"),
        ([{"my_chat_member": {"chat": {"id": 55}}}], "55"),
        ([{"update_id": 1}], None),
        ([], None),
        ("не список", None),
    ],
)
def test_extract_chat_id_variants(updates, expected):
    assert _extract_chat_id(updates) == expected
