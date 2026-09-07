import asyncio
from datetime import UTC, datetime

import pytest

from tg_collector.storage import Database, MessageRecord
from tg_collector.summarizer import (
    OpenAIChatClient,
    RetryLimitReached,
    SummaryJobInProgress,
    SummaryService,
)


class FakeAI:
    def __init__(self):
        self.calls: list[str] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(user_prompt)
        return "# 主题\n测试主题\n\n# 重要信息\n测试信息\n\n# 主要观点\n测试观点\n\n# 消息来源\n消息 ID 1、2"


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        message = type("Message", (), {"content": "响应内容"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeSDKClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": FakeCompletions()})()


class FailingAI:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("temporary failure")


class SlowAI(FakeAI):
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        await asyncio.sleep(0.02)
        return await super().complete(system_prompt=system_prompt, user_prompt=user_prompt)


class FailSecondCallOnceAI(FakeAI):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.attempts += 1
        self.calls.append(user_prompt)
        if self.attempts == 2:
            raise RuntimeError("second chunk failed")
        return "# 主题\n主题\n# 重要信息\n信息\n# 主要观点\n观点\n# 消息来源\n消息 ID 1"


def test_summary_is_chunked_written_and_reused_for_same_input(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="真实频道", username="hezu1", kind="channel")
    start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
    db.save_messages(
        [
            MessageRecord(1001, 1, start, 7, "甲" * 700, source_url="https://t.me/hezu1/1"),
            MessageRecord(1001, 2, start, 8, "乙" * 700, source_url="https://t.me/hezu1/2"),
        ]
    )
    ai = FakeAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="test-model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    first = asyncio.run(service.summarize_window(1001, start, end))
    second = asyncio.run(service.summarize_window(1001, start, end))

    assert first.output_path.exists()
    assert first.output_path.read_text(encoding="utf-8") == first.content
    assert first.output_path.stat().st_mode & 0o777 == 0o600
    assert first.output_path.parent.stat().st_mode & 0o777 == 0o700
    assert all(heading in first.content for heading in ("# 主题", "# 重要信息", "# 主要观点", "# 消息来源"))
    assert len(ai.calls) == 3  # two chunks and one merge
    assert all(len(prompt) <= 1000 for prompt in ai.calls)
    assert second.cached is True
    assert second.content == first.content


def test_openai_compatible_client_uses_configured_model():
    sdk = FakeSDKClient()
    client = OpenAIChatClient(model="custom-model", sdk_client=sdk)

    result = asyncio.run(client.complete(system_prompt="system", user_prompt="user"))

    assert result == "响应内容"
    assert sdk.chat.completions.kwargs["model"] == "custom-model"
    assert sdk.chat.completions.kwargs["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]


def test_summary_stops_retrying_after_configured_attempt_limit(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="hezu1", kind="channel")
    start = datetime(2026, 8, 29, 0, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "消息"))
    ai = FailingAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
        max_attempts=2,
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary failure"):
            asyncio.run(service.summarize_window(1001, start, end))
    with pytest.raises(RetryLimitReached):
        asyncio.run(service.summarize_window(1001, start, end))

    assert ai.calls == 2


def test_concurrent_summary_calls_atomically_claim_one_ai_execution(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "消息"))
    ai = SlowAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    async def run_both():
        return await asyncio.gather(
            service.summarize_window(1001, start, end),
            service.summarize_window(1001, start, end),
            return_exceptions=True,
        )

    results = asyncio.run(run_both())

    assert len(ai.calls) == 1
    assert sum(isinstance(item, SummaryJobInProgress) for item in results) == 1
    assert sum(not isinstance(item, Exception) for item in results) == 1


def test_retry_reuses_successful_persisted_chunk_response(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_messages(
        [
            MessageRecord(1001, 1, start, 7, "甲" * 700),
            MessageRecord(1001, 2, start, 8, "乙" * 700),
        ]
    )
    ai = FailSecondCallOnceAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
        max_attempts=3,
    )

    with pytest.raises(RuntimeError, match="second chunk failed"):
        asyncio.run(service.summarize_window(1001, start, end))
    result = asyncio.run(service.summarize_window(1001, start, end))

    assert result.cached is False
    assert ai.attempts == 4
    assert all(len(prompt) <= 1000 for prompt in ai.calls)


def test_changed_input_removes_stale_summary_before_retry(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "原消息"))
    first_service = SummaryService(
        database=db,
        ai=FakeAI(),
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )
    first = asyncio.run(first_service.summarize_window(1001, start, end))
    db.save_message(
        MessageRecord(
            1001,
            1,
            start,
            7,
            "编辑后消息",
            edited_at=start.replace(microsecond=1),
        )
    )
    retry_service = SummaryService(
        database=db,
        ai=FailingAI(),
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    with pytest.raises(RuntimeError, match="temporary failure"):
        asyncio.run(retry_service.summarize_window(1001, start, end))

    assert not first.output_path.exists()
    assert db.get_summary(first.job_id) is None
    assert db.status_counts()["latest_successful_summaries"] == []


def test_changed_input_unlinks_stale_summary_symlink_without_deleting_target(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "原消息"))
    service = SummaryService(
        database=db,
        ai=FakeAI(),
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )
    first = asyncio.run(service.summarize_window(1001, start, end))
    protected = first.output_path.parent / "protected.md"
    protected.write_text("不能删除", encoding="utf-8")
    first.output_path.unlink()
    first.output_path.symlink_to(protected)
    db.save_message(
        MessageRecord(
            1001,
            1,
            start,
            7,
            "编辑后消息",
            edited_at=start.replace(microsecond=1),
        )
    )
    failing = SummaryService(
        database=db,
        ai=FailingAI(),
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    with pytest.raises(RuntimeError, match="temporary failure"):
        asyncio.run(failing.summarize_window(1001, start, end))

    assert protected.exists()
    assert not first.output_path.is_symlink()


def test_summary_publish_rejects_symlinked_chat_output_directory(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "消息"))
    output_dir = tmp_path / "summaries"
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir.mkdir()
    (output_dir / "1001").symlink_to(outside, target_is_directory=True)
    service = SummaryService(
        database=db,
        ai=FakeAI(),
        output_dir=output_dir,
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    with pytest.raises(ValueError, match="outside configured output directory"):
        asyncio.run(service.summarize_window(1001, start, end))

    assert list(outside.iterdir()) == []


def test_missing_summary_output_is_regenerated_instead_of_returned_from_cache(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "消息"))
    ai = FakeAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    first = asyncio.run(service.summarize_window(1001, start, end))
    first.output_path.unlink()
    second = asyncio.run(service.summarize_window(1001, start, end))

    assert second.cached is False
    assert second.output_path.exists()
    assert len(ai.calls) == 2


def test_damaged_summary_output_is_regenerated_instead_of_returned_from_cache(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, start, 7, "消息"))
    ai = FakeAI()
    service = SummaryService(
        database=db,
        ai=ai,
        output_dir=tmp_path / "summaries",
        model="model",
        max_input_chars=1000,
        prompt_version="v1",
        timezone="Asia/Shanghai",
    )

    first = asyncio.run(service.summarize_window(1001, start, end))
    first.output_path.write_text("损坏内容", encoding="utf-8")
    second = asyncio.run(service.summarize_window(1001, start, end))

    assert second.cached is False
    assert first.output_path.read_text(encoding="utf-8") == second.content
    assert second.output_path.read_text(encoding="utf-8") == second.content
    assert len(ai.calls) == 2
