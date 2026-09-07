import asyncio
import json
import sys
from datetime import UTC, datetime

import pytest

from tg_collector.cli import (
    _collector,
    _run_due_cleanup,
    _secure_session_files,
    _summary_service,
    build_parser,
    main,
    run_cli,
)
from tg_collector.config import load_config
from tg_collector.storage import Database, MessageRecord


def _write_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
timezone: Asia/Shanghai
database_path: data/telegram.sqlite3
summary_dir: data/summaries
retention_days: 6
summary_times: ["08:00", "12:00", "22:00"]
cleanup: {weekday: 6, time: "03:00"}
telegram:
  session_path: data/telegram
  allowlist: ["@hezu1"]
  shop_url_channels: ["@hezu1"]
ai:
  max_input_chars: 12000
  timeout_seconds: 90
  max_retries: 3
  prompt_version: v1
""".strip(),
        encoding="utf-8",
    )
    return path


def test_collector_receives_special_channel_configuration(tmp_path):
    config = load_config(_write_config(tmp_path))
    database = Database(config.database_path)

    collector = _collector(config, database, object())

    assert collector.shop_url_channels == ("@hezu1",)


def test_main_handles_keyboard_interrupt_as_clean_exit(monkeypatch, capsys):
    def interrupted(_argv):
        raise KeyboardInterrupt

    monkeypatch.setattr("tg_collector.cli.run_cli", interrupted)

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 0
    assert capsys.readouterr().err == "stopped by user\n"


def test_summary_service_treats_max_retries_as_retries_after_first_attempt(tmp_path):
    config = load_config(
        _write_config(tmp_path),
        environ={
            "TG_COLLECTOR_OPENAI_BASE_URL": "https://ai.example/v1",
            "TG_COLLECTOR_OPENAI_API_KEY": "secret",
            "TG_COLLECTOR_OPENAI_MODEL": "model",
        },
    )

    service = _summary_service(config, Database(config.database_path))

    assert service.max_attempts == 4


def test_init_db_and_status_work_without_external_credentials(tmp_path, capsys):
    config_path = _write_config(tmp_path)

    assert run_cli(["--config", str(config_path), "init-db"]) == 0
    capsys.readouterr()
    assert run_cli(["--config", str(config_path), "status"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert status["configured_sources"] == 1
    assert status["resolved_sources"] == 0
    assert status["messages"] == 0
    assert status["summary_jobs"] == {
        "pending": 0,
        "running": 0,
        "failed": 0,
        "completed": 0,
    }
    assert status["sources"] == []
    assert status["last_cleanup"] is None
    assert status["telegram_authorization"] == "not_logged_in"
    assert "next_summary_execution" in status
    assert "next_summary_window" in status
    assert status["latest_successful_summaries"] == []
    assert status["database_exists"] is True


def test_cleanup_dry_run_does_not_delete(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    run_cli(["--config", str(config_path), "init-db"])
    capsys.readouterr()
    db = Database(tmp_path / "data/telegram.sqlite3")
    db.upsert_chat(chat_id=1001, title="频道", username="hezu1", kind="channel")
    db.save_message(
        MessageRecord(
            1001,
            1,
            datetime(2026, 8, 20, tzinfo=UTC),
            7,
            "过期消息",
        )
    )

    assert (
        run_cli(
            [
                "--config",
                str(config_path),
                "cleanup",
                "--dry-run",
                "--now",
                "2026-08-30T03:00:00+08:00",
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["deleted_messages"] == 1
    assert result["dry_run"] is True
    assert db.get_message(1001, 1) is not None


def test_parser_exposes_required_operational_commands():
    parser = build_parser()

    assert parser.parse_args(["login"]).command == "login"
    assert parser.parse_args(["sources", "list"]).sources_command == "list"
    assert parser.parse_args(["sources", "validate"]).sources_command == "validate"
    for command in ("backfill", "run", "summarize", "scheduler"):
        assert parser.parse_args([command]).command == command
    assert parser.parse_args(["backup", "--destination", "backup.sqlite3"]).command == "backup"


def test_backup_creates_readable_database_copy(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    run_cli(["--config", str(config_path), "init-db"])
    capsys.readouterr()
    db = Database(tmp_path / "data/telegram.sqlite3")
    db.upsert_chat(chat_id=1001, title="频道", username="hezu1", kind="channel")
    db.save_message(MessageRecord(1001, 1, datetime.now(UTC), 7, "备份消息"))
    destination = tmp_path / "backups" / "telegram.sqlite3"

    assert (
        run_cli(
            [
                "--config",
                str(config_path),
                "backup",
                "--destination",
                str(destination),
            ]
        )
        == 0
    )

    assert Database(destination).get_message(1001, 1).text == "备份消息"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700


def test_main_reports_missing_ai_configuration_without_traceback(
    tmp_path, capsys, monkeypatch
):
    config_path = _write_config(tmp_path)
    for key in (
        "TG_COLLECTOR_OPENAI_BASE_URL",
        "TG_COLLECTOR_OPENAI_API_KEY",
        "TG_COLLECTOR_OPENAI_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["tg-collector", "--config", str(config_path), "summarize"]
    )

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 2
    stderr = capsys.readouterr().err
    assert "missing AI configuration" in stderr
    assert "Traceback" not in stderr


def test_secure_session_files_restricts_file_without_chmodding_existing_parent(tmp_path):
    session_stem = tmp_path / "private" / "telegram"
    session_stem.parent.mkdir()
    session_file = session_stem.with_suffix(".session")
    session_file.write_text("session", encoding="utf-8")
    session_file.chmod(0o644)
    session_stem.parent.chmod(0o755)

    _secure_session_files(session_stem)

    assert session_file.stat().st_mode & 0o777 == 0o600
    assert session_stem.parent.stat().st_mode & 0o777 == 0o755


def test_due_cleanup_catches_up_after_schedule_and_runs_once_per_boundary(tmp_path):
    config = load_config(_write_config(tmp_path), environ={})
    db = Database(config.database_path)
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="hezu1", kind="channel")
    db.save_message(
        MessageRecord(1001, 1, datetime(2020, 1, 1, tzinfo=UTC), 7, "过期消息")
    )
    now = datetime.now(UTC)

    assert _run_due_cleanup(config, db, now) is True
    assert db.get_message(1001, 1) is None
    first_completed_at = db.status_counts()["last_cleanup"]["completed_at"]

    assert _run_due_cleanup(config, db, now) is False
    assert db.status_counts()["last_cleanup"]["completed_at"] == first_completed_at


def test_scheduler_runs_cleanup_even_when_ai_configuration_is_missing(
    tmp_path, monkeypatch
):
    config = load_config(_write_config(tmp_path), environ={})
    db = Database(config.database_path)
    db.initialize()
    cleanup_calls = []

    def fake_cleanup(config_arg, database_arg, now):
        cleanup_calls.append((config_arg, database_arg, now))
        return True

    def missing_ai(*args, **kwargs):
        raise ValueError("missing AI configuration")

    async def stop_sleep(interval):
        raise RuntimeError("stop scheduler test")

    monkeypatch.setattr("tg_collector.cli._run_due_cleanup", fake_cleanup)
    monkeypatch.setattr("tg_collector.cli._summary_service", missing_ai)
    monkeypatch.setattr("tg_collector.cli.asyncio.sleep", stop_sleep)

    with pytest.raises(RuntimeError, match="stop scheduler test"):
        asyncio.run(__import__("tg_collector.cli", fromlist=["_scheduler"])._scheduler(config, db, 1))

    assert len(cleanup_calls) == 1
