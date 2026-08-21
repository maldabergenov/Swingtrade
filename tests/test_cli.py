from __future__ import annotations

import pytest

from swingscan.cli import build_parser, main


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_scan_flags_are_parsed():
    args = build_parser().parse_args(["--limit", "50", "scan", "--dry-run", "--no-cache"])
    assert args.command == "scan"
    assert args.limit == 50
    assert args.dry_run is True
    assert args.no_cache is True


def test_serve_flags_are_parsed():
    args = build_parser().parse_args(["serve", "--run-now"])
    assert args.command == "serve" and args.run_now is True


def test_schedule_command_prints_next_runs(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    assert main(["schedule"]) == 0
    out = capsys.readouterr().out
    assert "Asia/Almaty" in out
    assert "09:00" in out and "21:00" in out


def test_scan_command_runs_pipeline(monkeypatch, capsys, tmp_path):
    from swingscan.models import FunnelStats, ScanResult
    from datetime import datetime, timezone

    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))

    now = datetime.now(timezone.utc)
    empty = ScanResult(started_at=now, finished_at=now, as_of=None, funnel=FunnelStats())
    monkeypatch.setattr("swingscan.app.Scanner", lambda *a, **k: type("S", (), {"run": lambda self, n=None: empty})())

    assert main(["scan", "--dry-run"]) == 0
    assert "Swing-сканер рынка США" in capsys.readouterr().out


def test_telegram_test_command_reports_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["telegram-test"]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_telegram_test_command_sends(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    sent: list[str] = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def get_me(self):
            return {"id": 1, "username": "swing_bot"}

        def resolve_chat_id(self):
            return "42"

        def send_message(self, text, chat_id=None):
            sent.append(text)
            return [{"message_id": 1}]

    monkeypatch.setattr("swingscan.cli.TelegramClient", Fake)
    assert main(["telegram-test"]) == 0
    assert sent and "swingscan" in sent[0]
    assert "@swing_bot" in capsys.readouterr().out


def test_universe_command_lists_symbols(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "swingscan.universe._download",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("нет сети")),
    )
    assert main(["universe"]) == 0
    assert "Бумаг во вселенной" in capsys.readouterr().out


def test_limit_flag_reaches_settings(monkeypatch, tmp_path):
    from swingscan.cli import _settings_from_args

    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    args = build_parser().parse_args(["--limit", "7", "scan", "--no-cache"])
    settings = _settings_from_args(args)
    assert settings.filters.max_symbols == 7
    assert settings.data.use_cache is False
