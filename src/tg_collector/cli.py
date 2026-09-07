from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telethon import TelegramClient

from .app import summarize_due
from .config import AppConfig, load_config
from .schedule import latest_weekly_cleanup, next_summary_window, retention_cutoff
from .storage import Database
from .summarizer import OpenAIChatClient, SummaryService
from .telegram import TelegramCollector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-collector")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="初始化本地 SQLite 数据库")
    subcommands.add_parser("status", help="输出运行状态")
    subcommands.add_parser("login", help="交互登录 Telegram 并保存会话")
    sources = subcommands.add_parser("sources", help="管理人工白名单来源")
    source_commands = sources.add_subparsers(dest="sources_command", required=True)
    source_commands.add_parser("list", help="列出配置和已解析来源")
    source_commands.add_parser("validate", help="连接 Telegram 验证白名单")
    subcommands.add_parser("backfill", help="回填最近保留窗口消息")
    subcommands.add_parser("run", help="持续采集白名单消息")
    summarize = subcommands.add_parser("summarize", help="总结所有已关闭窗口")
    summarize.add_argument("--now", help="测试用 ISO 时间；默认使用当前时间")
    scheduler = subcommands.add_parser("scheduler", help="持续运行总结和清理调度")
    scheduler.add_argument("--interval", type=float, default=60.0)
    backup = subcommands.add_parser("backup", help="创建一致性 SQLite 备份")
    backup.add_argument("--destination", required=True, help="备份文件路径")
    cleanup = subcommands.add_parser("cleanup", help="清理到期消息和总结")
    cleanup.add_argument("--dry-run", action="store_true", help="只预览删除数量")
    cleanup.add_argument("--now", help="测试用 ISO 时间；默认使用当前时间")
    return parser


def _require_telegram(config) -> None:
    missing = []
    if config.telegram.api_id is None:
        missing.append("TG_COLLECTOR_TELEGRAM_API_ID")
    if not config.telegram.api_hash:
        missing.append("TG_COLLECTOR_TELEGRAM_API_HASH")
    if missing:
        raise ValueError(f"missing Telegram configuration: {', '.join(missing)}")


def _secure_session_files(session_path: Path) -> None:
    """Restrict the Telegram session directory and SQLite files to the owner."""
    session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_file = _session_file_path(session_path)
    for candidate in (
        session_file,
        Path(f"{session_file}-journal"),
        Path(f"{session_file}-wal"),
        Path(f"{session_file}-shm"),
    ):
        if candidate.exists():
            candidate.chmod(0o600)


def _session_file_path(session_path: Path) -> Path:
    return (
        session_path
        if session_path.suffix == ".session"
        else session_path.with_suffix(".session")
    )


def _telegram_client(config) -> TelegramClient:
    _require_telegram(config)
    _secure_session_files(config.telegram.session_path)
    return TelegramClient(
        str(config.telegram.session_path),
        config.telegram.api_id,
        config.telegram.api_hash,
    )


def _collector(config, database: Database, client: TelegramClient) -> TelegramCollector:
    return TelegramCollector(
        client=client,
        database=database,
        allowlist=config.telegram.allowlist,
        shop_url_channels=config.telegram.shop_url_channels,
        retention_days=config.retention_days,
        timezone=ZoneInfo(config.timezone),
    )


async def _connect_authorized(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("Telegram session is not authorized; run `tg-collector login` first")


async def _login(config) -> None:
    client = _telegram_client(config)
    try:
        await client.start(phone=config.telegram.phone)
        me = await client.get_me()
        print(json.dumps({"authorized": True, "user_id": me.id}, ensure_ascii=False))
    finally:
        await client.disconnect()
        _secure_session_files(config.telegram.session_path)


async def _validate_sources(config, database: Database) -> None:
    client = _telegram_client(config)
    await _connect_authorized(client)
    try:
        sources = await _collector(config, database, client).resolve_sources()
        print(
            json.dumps(
                [
                    {
                        "chat_id": item.chat_id,
                        "title": item.title,
                        "username": item.username,
                        "kind": item.kind,
                    }
                    for item in sources
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await client.disconnect()


async def _backfill(config, database: Database) -> None:
    client = _telegram_client(config)
    await _connect_authorized(client)
    try:
        count = await _collector(config, database, client).backfill()
        print(json.dumps({"saved_messages": count}, ensure_ascii=False))
    finally:
        await client.disconnect()


async def _run_collector(config, database: Database) -> None:
    client = _telegram_client(config)
    await _connect_authorized(client)
    try:
        await _collector(config, database, client).run_forever()
    finally:
        await client.disconnect()


def _summary_service(config, database: Database) -> SummaryService:
    missing = []
    if not config.ai.base_url:
        missing.append("TG_COLLECTOR_OPENAI_BASE_URL")
    if not config.ai.api_key:
        missing.append("TG_COLLECTOR_OPENAI_API_KEY")
    if not config.ai.model:
        missing.append("TG_COLLECTOR_OPENAI_MODEL")
    if missing:
        raise ValueError(f"missing AI configuration: {', '.join(missing)}")
    ai = OpenAIChatClient(
        base_url=config.ai.base_url,
        api_key=config.ai.api_key,
        model=config.ai.model,
        timeout_seconds=config.ai.timeout_seconds,
        # SummaryService owns the durable retry budget and chunk cache.
        max_retries=0,
    )
    return SummaryService(
        database=database,
        ai=ai,
        output_dir=config.summary_dir,
        model=config.ai.model,
        max_input_chars=config.ai.max_input_chars,
        prompt_version=config.ai.prompt_version,
        timezone=config.timezone,
        # max_retries counts retries after the initial request.
        max_attempts=config.ai.max_retries + 1,
    )


async def _summarize(config, database: Database, now: datetime) -> int:
    return await summarize_due(
        database=database,
        service=_summary_service(config, database),
        now=now,
        retention_days=config.retention_days,
        schedule_times=config.summary_times,
        timezone=ZoneInfo(config.timezone),
    )


def _run_due_cleanup(config: AppConfig, database: Database, now: datetime) -> bool:
    timezone = ZoneInfo(config.timezone)
    due_boundary = latest_weekly_cleanup(
        now,
        weekday=config.cleanup.weekday,
        at=config.cleanup.time,
        tz=timezone,
    )
    last_cleanup = database.status_counts()["last_cleanup"]
    if isinstance(last_cleanup, dict):
        completed_at = datetime.fromisoformat(str(last_cleanup["completed_at"]))
        if completed_at >= due_boundary.astimezone(UTC):
            return False
    cutoff = retention_cutoff(now, retention_days=config.retention_days, tz=timezone)
    database.cleanup_expired(cutoff, summary_dir=config.summary_dir)
    return True


async def _scheduler(config: AppConfig, database: Database, interval: float) -> None:
    if interval <= 0:
        raise ValueError("scheduler interval must be positive")
    timezone = ZoneInfo(config.timezone)
    summary_task: asyncio.Task[object] | None = None
    try:
        while True:
            now = datetime.now(UTC)
            try:
                _run_due_cleanup(config, database, now)
            except Exception as exc:  # noqa: BLE001 - scheduler must keep running
                print(f"cleanup scheduler error: {exc}", file=sys.stderr)

            if summary_task is not None and summary_task.done():
                try:
                    summary_task.result()
                except Exception as exc:  # noqa: BLE001 - report one failed run
                    print(f"summary scheduler error: {exc}", file=sys.stderr)
                summary_task = None

            if summary_task is None:
                try:
                    service = _summary_service(config, database)
                except Exception as exc:  # noqa: BLE001 - cleanup must not depend on AI
                    print(f"summary scheduler error: {exc}", file=sys.stderr)
                else:
                    summary_task = asyncio.create_task(
                        summarize_due(
                            database=database,
                            service=service,
                            now=now,
                            retention_days=config.retention_days,
                            schedule_times=config.summary_times,
                            timezone=timezone,
                        )
                    )
            await asyncio.sleep(interval)
    finally:
        if summary_task is not None and not summary_task.done():
            summary_task.cancel()
            with suppress(asyncio.CancelledError):
                await summary_task


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config))
    database = Database(config.database_path)

    if args.command == "init-db":
        database.initialize()
        print(f"initialized: {config.database_path}")
        return 0

    if args.command == "status":
        database.initialize()
        status = database.status_counts()
        now = datetime.now(UTC)
        next_start, next_end = next_summary_window(
            now, config.summary_times, ZoneInfo(config.timezone)
        )
        session_file = _session_file_path(config.telegram.session_path)
        status.update(
            {
                "configured_sources": len(config.telegram.allowlist),
                "telegram_authorization": (
                    "session_present_unverified"
                    if session_file.exists()
                    else "not_logged_in"
                ),
                "next_summary_execution": next_end.astimezone(UTC).isoformat(),
                "next_summary_window": {
                    "start": next_start.astimezone(UTC).isoformat(),
                    "end": next_end.astimezone(UTC).isoformat(),
                },
                "database_exists": config.database_path.exists(),
                "database_bytes": (
                    config.database_path.stat().st_size
                    if config.database_path.exists()
                    else 0
                ),
            }
        )
        print(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.command == "login":
        asyncio.run(_login(config))
        return 0

    if args.command == "sources":
        database.initialize()
        if args.sources_command == "list":
            print(
                json.dumps(
                    {
                        "configured": list(config.telegram.allowlist),
                        "resolved": [
                            {
                                "chat_id": chat.chat_id,
                                "title": chat.title,
                                "username": chat.username,
                                "kind": chat.kind,
                            }
                            for chat in database.list_chats(enabled_only=False)
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        asyncio.run(_validate_sources(config, database))
        return 0

    if args.command == "backfill":
        database.initialize()
        asyncio.run(_backfill(config, database))
        return 0

    if args.command == "run":
        database.initialize()
        asyncio.run(_run_collector(config, database))
        return 0

    if args.command == "summarize":
        database.initialize()
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("--now must include a timezone offset")
        count = asyncio.run(_summarize(config, database, now))
        print(json.dumps({"summarized_windows": count}, ensure_ascii=False))
        return 0

    if args.command == "scheduler":
        database.initialize()
        asyncio.run(_scheduler(config, database, args.interval))
        return 0

    if args.command == "backup":
        database.initialize()
        destination = database.backup(args.destination)
        print(json.dumps({"backup": str(destination)}, ensure_ascii=False))
        return 0

    if args.command == "cleanup":
        database.initialize()
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("--now must include a timezone offset")
        cutoff = retention_cutoff(
            now,
            retention_days=config.retention_days,
            tz=ZoneInfo(config.timezone),
        )
        result = (
            database.preview_cleanup(cutoff)
            if args.dry_run
            else database.cleanup_expired(cutoff, summary_dir=config.summary_dir)
        )
        print(
            json.dumps(
                {
                    "cutoff": cutoff.isoformat(),
                    "deleted_messages": result.deleted_messages,
                    "deleted_summaries": result.deleted_summaries,
                    "dry_run": args.dry_run,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    raise ValueError(f"unsupported command: {args.command}")


def main() -> None:
    try:
        exit_code = run_cli(sys.argv[1:])
    except KeyboardInterrupt:
        print("stopped by user", file=sys.stderr)
        raise SystemExit(0) from None
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to safe exit
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)