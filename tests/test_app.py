import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tg_collector.app import summarize_due
from tg_collector.storage import Database, MessageRecord


class FakeSummaryService:
    def __init__(self):
        self.calls = []

    async def summarize_window(self, chat_id, window_start, window_end):
        self.calls.append((chat_id, window_start, window_end))


class FailingFirstSummaryService(FakeSummaryService):
    async def summarize_window(self, chat_id, window_start, window_end):
        await super().summarize_window(chat_id, window_start, window_end)
        if chat_id == 1001:
            raise RuntimeError("AI unavailable")


def test_summarize_due_only_runs_closed_windows_that_have_messages(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="hezu1", kind="channel")
    db.save_messages(
        [
            MessageRecord(
                1001,
                1,
                datetime(2026, 8, 28, 23, 0, tzinfo=tz).astimezone(UTC),
                7,
                "夜间消息",
            ),
            MessageRecord(
                1001,
                2,
                datetime(2026, 8, 29, 9, 0, tzinfo=tz).astimezone(UTC),
                7,
                "上午消息",
            ),
        ]
    )
    service = FakeSummaryService()

    count = asyncio.run(
        summarize_due(
            database=db,
            service=service,
            now=datetime(2026, 8, 29, 12, 0, tzinfo=tz),
            retention_days=6,
            schedule_times=("08:00", "12:00", "22:00"),
            timezone=tz,
        )
    )

    assert count == 2
    assert [(start, end) for _, start, end in service.calls] == [
        (
            datetime(2026, 8, 28, 22, 0, tzinfo=tz),
            datetime(2026, 8, 29, 8, 0, tzinfo=tz),
        ),
        (
            datetime(2026, 8, 29, 8, 0, tzinfo=tz),
            datetime(2026, 8, 29, 12, 0, tzinfo=tz),
        ),
    ]


def test_summarize_due_continues_after_one_source_fails(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    window_message_time = datetime(2026, 8, 29, 9, 0, tzinfo=tz).astimezone(UTC)
    for chat_id in (1001, 1002):
        db.upsert_chat(
            chat_id=chat_id,
            title=f"频道{chat_id}",
            username=f"channel{chat_id}",
            kind="channel",
        )
        db.save_message(MessageRecord(chat_id, 1, window_message_time, 7, "消息"))
    service = FailingFirstSummaryService()

    count = asyncio.run(
        summarize_due(
            database=db,
            service=service,
            now=datetime(2026, 8, 29, 12, 0, tzinfo=tz),
            retention_days=6,
            schedule_times=("08:00", "12:00", "22:00"),
            timezone=tz,
        )
    )

    assert count == 1
    assert [chat_id for chat_id, _, _ in service.calls] == [1001, 1002]


def test_summarize_due_clips_window_that_crosses_retention_cutoff(tmp_path):
    tz = ZoneInfo("Asia/Shanghai")
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    db.save_messages(
        [
            MessageRecord(
                1001,
                1,
                datetime(2026, 8, 23, 23, 30, tzinfo=tz).astimezone(UTC),
                7,
                "已经超过逻辑保留期",
            ),
            MessageRecord(
                1001,
                2,
                datetime(2026, 8, 24, 4, 0, tzinfo=tz).astimezone(UTC),
                7,
                "最早保留日的窗口内消息",
            ),
        ]
    )
    service = FakeSummaryService()

    count = asyncio.run(
        summarize_due(
            database=db,
            service=service,
            now=datetime(2026, 8, 29, 8, 0, tzinfo=tz),
            retention_days=6,
            schedule_times=("08:00", "12:00", "22:00"),
            timezone=tz,
        )
    )

    assert count == 1
    assert service.calls == [
        (
            1001,
            datetime(2026, 8, 24, 0, 0, tzinfo=tz),
            datetime(2026, 8, 24, 8, 0, tzinfo=tz),
        )
    ]
