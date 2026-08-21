from __future__ import annotations

import json
from datetime import datetime, timezone


from swingscan.app import deliver, run_once, run_scan
from swingscan.models import FunnelStats, ScanResult
from swingscan.telegram import TelegramError
from tests.test_report import make_idea


class FakeScanner:
    def __init__(self, ideas=None, errors=None):
        self.ideas = ideas or []
        self.errors = errors or []
        self.calls = 0

    def run(self, now=None):
        self.calls += 1
        started = now or datetime.now(timezone.utc)
        return ScanResult(
            started_at=started,
            finished_at=started,
            as_of=None,
            ideas=self.ideas,
            funnel=FunnelStats(universe=10, with_data=10),
            errors=self.errors,
        )


class FakeTelegram:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    def send_message(self, text, chat_id=None):
        if self.fail:
            raise TelegramError("chat not found")
        self.sent.append(text)
        return [{"message_id": len(self.sent)}]


def test_run_scan_creates_directories(settings):
    run_scan(settings, FakeScanner())
    assert settings.state_dir.exists()
    assert settings.reports_dir.exists()


def test_deliver_sends_and_saves(settings, capsys):
    scanner = FakeScanner(ideas=[make_idea()])
    result = run_scan(settings, scanner)
    telegram = FakeTelegram()
    assert deliver(result, settings, client=telegram) is True
    assert len(telegram.sent) == 1
    assert "AAPL" in telegram.sent[0]

    reports = list(settings.reports_dir.glob("scan_*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["ideas"][0]["symbol"] == "AAPL"
    assert "AAPL" in capsys.readouterr().out


def test_dry_run_does_not_send(settings):
    result = run_scan(settings, FakeScanner(ideas=[make_idea()]))
    telegram = FakeTelegram()
    assert deliver(result, settings, dry_run=True, client=telegram) is False
    assert telegram.sent == []


def test_empty_report_can_be_suppressed(settings):
    quiet = settings.with_overrides(send_empty_report=False)
    result = run_scan(quiet, FakeScanner(ideas=[]))
    telegram = FakeTelegram()
    assert deliver(result, quiet, client=telegram) is False
    assert telegram.sent == []


def test_empty_report_is_sent_by_default(settings):
    result = run_scan(settings, FakeScanner(ideas=[]))
    telegram = FakeTelegram()
    assert deliver(result, settings, client=telegram) is True
    assert "Подходящих сетапов нет" in telegram.sent[0]


def test_telegram_failure_does_not_crash_the_run(settings):
    result = run_scan(settings, FakeScanner(ideas=[make_idea()]))
    assert deliver(result, settings, client=FakeTelegram(fail=True)) is False


def test_missing_token_falls_back_to_console(settings):
    result = run_scan(settings, FakeScanner(ideas=[make_idea()]))
    assert settings.telegram.enabled is False
    assert deliver(result, settings) is False


def test_run_once_scans_then_delivers(settings):
    scanner = FakeScanner(ideas=[make_idea()])
    telegram = FakeTelegram()
    result = run_once(settings, scanner=scanner, client=telegram)
    assert scanner.calls == 1
    assert len(telegram.sent) == 1
    assert len(result.ideas) == 1


def test_report_save_failure_does_not_block_sending(settings, monkeypatch):
    monkeypatch.setattr(
        "swingscan.app.save_report",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("диск переполнен")),
    )
    result = run_scan(settings, FakeScanner(ideas=[make_idea()]))
    telegram = FakeTelegram()
    assert deliver(result, settings, client=telegram) is True
