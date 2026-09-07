import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from telethon import utils
from telethon.tl.types import Channel, ChatPhotoEmpty, Message, PeerChannel, PeerUser

from tg_collector.storage import Database, MessageRecord
from tg_collector.telegram import (
    Source,
    TelegramCollector,
    extract_shop_urls,
    message_link_url,
    normalize_source_reference,
)


class FakeTelegramClient:
    def __init__(self, entity, messages):
        self.entity = entity
        self.messages = messages
        self.requested = []
        self.iter_requests = []
        self.get_requests = []

    async def get_entity(self, reference):
        self.requested.append(reference)
        return self.entity

    def iter_messages(self, entity, **kwargs):
        self.iter_requests.append((entity, kwargs))

        async def generate():
            yielded = 0
            for message in self.messages:
                if message.id <= kwargs.get("min_id", 0):
                    continue
                if yielded >= kwargs.get("limit", float("inf")):
                    break
                yield message
                yielded += 1

        return generate()

    async def get_messages(self, entity, *, ids):
        self.get_requests.append((entity, tuple(ids)))
        requested = set(ids)
        return [message for message in self.messages if message.id in requested]


class DisconnectingTelegramClient(FakeTelegramClient):
    def __init__(self, entity, messages, missed_message):
        super().__init__(entity, messages)
        self.missed_message = missed_message
        self.handlers = []

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    async def run_until_disconnected(self):
        self.messages.insert(0, self.missed_message)
        await asyncio.sleep(0.02)


class OfflineAfterDisconnectClient(DisconnectingTelegramClient):
    def __init__(self, entity, messages, missed_message):
        super().__init__(entity, messages, missed_message)
        self.connected = True

    def is_connected(self):
        return self.connected

    async def run_until_disconnected(self):
        self.connected = False
        await super().run_until_disconnected()


class PartiallyFailingTelegramClient(FakeTelegramClient):
    async def get_entity(self, reference):
        self.requested.append(reference)
        if reference == "@broken":
            raise RuntimeError("unavailable source")
        return self.entity


class InvalidEntityTelegramClient(FakeTelegramClient):
    async def get_entity(self, reference):
        self.requested.append(reference)
        if reference == "@invalid":
            return object()
        return self.entity


class BioTelegramClient(FakeTelegramClient):
    async def get_entity(self, reference):
        if isinstance(reference, int):
            return type("User", (), {"about": "店铺用户简介"})()
        return await super().get_entity(reference)


class FlakyBioTelegramClient(FakeTelegramClient):
    def __init__(self, entity, messages):
        super().__init__(entity, messages)
        self.bio_attempts = 0

    async def get_entity(self, reference):
        if isinstance(reference, int):
            self.bio_attempts += 1
            if self.bio_attempts == 1:
                raise RuntimeError("temporary profile failure")
            return type("User", (), {"about": "重试取得的简介"})()
        return await super().get_entity(reference)


class ChannelSenderBioTelegramClient(FakeTelegramClient):
    def __init__(self, entity, messages, sender_channel):
        super().__init__(entity, messages)
        self.sender_channel = sender_channel
        self.full_user_requests = 0

    async def get_entity(self, reference):
        if isinstance(reference, int):
            return self.sender_channel
        return await super().get_entity(reference)

    async def __call__(self, request):
        self.full_user_requests += 1
        raise AssertionError("channel senders do not have a user bio")


class PerEntityTelegramClient(FakeTelegramClient):
    def iter_messages(self, entity, **kwargs):
        if entity == "broken-entity":
            raise RuntimeError("source iteration failed")
        return super().iter_messages(entity, **kwargs)


def test_normalize_source_reference_accepts_username_and_tme_url():
    assert normalize_source_reference("@hezu1") == "@hezu1"
    assert normalize_source_reference("https://t.me/hezu1") == "@hezu1"


def test_normalize_source_reference_preserves_numeric_chat_id_as_integer():
    assert normalize_source_reference("-1001234567890") == -1001234567890


@pytest.mark.parametrize(
    "reference",
    [
        "https://t.me/c/123/42",
        "https://t.me/c",
        "https://t.me/C",
        "https://t.me/s/channel",
        "https://t.me/s",
        "https://t.me/S",
        "https://t.me/joinchat/AAAAAE",
        "https://t.me/channel/42",
        "https://t.me/channel?start=1",
        "https://t.me/channel#fragment",
        "https://user:pass@t.me/channel",
        "https://t.me:443/channel",
        "ftp://t.me/validname",
        "https://example.com/channel",
        "@bad/name",
        "plain-channel",
    ],
)
def test_normalize_source_reference_rejects_message_and_invite_paths(reference):
    with pytest.raises(ValueError):
        normalize_source_reference(reference)


def test_extract_shop_urls_accepts_domains_with_or_without_scheme_and_deduplicates():
    text = "shop.example/item https://shop.example/item www.example.org/a，shop.example/item"

    assert extract_shop_urls(text) == (
        "https://shop.example/item",
        "https://www.example.org/a",
    )


def test_extract_shop_urls_rejects_decimal_prices_as_domains():
    text = "价格 0.85，折扣 0.15，套餐 11.30；店铺 shop.example/item"

    assert extract_shop_urls(text) == ("https://shop.example/item",)


def test_message_link_url_supports_public_and_private_supergroup_sources():
    public = Source(1001, "公开", "channel", "channel", object())
    private = Source(-1000000000123, "私有", None, "group", object())

    assert message_link_url(public, 42) == "https://t.me/channel/42"
    assert message_link_url(private, 42) == "https://t.me/c/123/42"


def test_backfill_only_saves_text_inside_retention_window(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    messages = [
        Message(3, PeerChannel(123), date=datetime(2026, 8, 29, 1, tzinfo=UTC), message="最新"),
        Message(2, PeerChannel(123), date=datetime(2026, 8, 28, 1, tzinfo=UTC), message="保留"),
        Message(1, PeerChannel(123), date=datetime(2026, 8, 20, 1, tzinfo=UTC), message="过期"),
    ]
    client = FakeTelegramClient(channel, messages)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("https://t.me/hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
        batch_size=2,
    )

    count = asyncio.run(
        collector.backfill(now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    )

    chat_id = utils.get_peer_id(channel)
    assert count == 2
    assert client.requested == ["@hezu1"]
    assert db.get_message(chat_id, 3).text == "最新"
    assert db.get_message(chat_id, 2).text == "保留"
    assert db.get_message(chat_id, 1) is None


def test_special_channel_saves_urls_and_bio_without_sender_identity(tmp_path):
    channel = Channel(
        id=123,
        title="店铺频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="shopchannel",
    )
    message = Message(
        3,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 1, tzinfo=UTC),
        message="shop.example/item https://shop.example/item",
        from_id=PeerUser(77),
    )
    client = BioTelegramClient(channel, [message])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@shopchannel",),
        shop_url_channels=("@shopchannel",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    asyncio.run(
        collector.backfill(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    chat_id = utils.get_peer_id(channel)
    stored = db.get_message(chat_id, 3)
    assert stored.message_url is None
    assert stored.sender_id is None
    assert stored.sender_bio == "店铺用户简介"
    assert db.get_message_urls(chat_id, 3) == ["https://shop.example/item"]


def test_special_channel_retries_bio_after_temporary_failure(tmp_path):
    channel = Channel(
        id=123,
        title="店铺频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="shopchannel",
    )
    messages = [
        Message(
            2,
            PeerChannel(123),
            date=datetime(2026, 8, 29, 2, tzinfo=UTC),
            message="first.example",
            from_id=PeerUser(77),
        ),
        Message(
            1,
            PeerChannel(123),
            date=datetime(2026, 8, 29, 1, tzinfo=UTC),
            message="second.example",
            from_id=PeerUser(77),
        ),
    ]
    client = FlakyBioTelegramClient(channel, messages)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@shopchannel",),
        shop_url_channels=("@shopchannel",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    asyncio.run(
        collector.backfill(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    chat_id = utils.get_peer_id(channel)
    assert db.get_message(chat_id, 2).sender_bio is None
    assert db.get_message(chat_id, 1).sender_bio == "重试取得的简介"
    assert client.bio_attempts == 2


def test_special_channel_skips_bio_lookup_for_channel_sender(tmp_path):
    channel = Channel(
        id=123,
        title="店铺频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="shopchannel",
    )
    sender_channel = Channel(
        id=456,
        title="匿名频道身份",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="anonymouschannel",
    )
    message = Message(
        3,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 1, tzinfo=UTC),
        message="shop.example/item",
        from_id=PeerChannel(456),
    )
    client = ChannelSenderBioTelegramClient(channel, [message], sender_channel)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@shopchannel",),
        shop_url_channels=("@shopchannel",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    asyncio.run(
        collector.backfill(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    stored = db.get_message(utils.get_peer_id(channel), 3)
    assert stored.sender_id is None
    assert stored.sender_bio is None
    assert client.full_user_requests == 0


def test_incremental_ingest_only_accepts_resolved_allowlist_and_keeps_edits(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    client = FakeTelegramClient(channel, [])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())
    chat_id = utils.get_peer_id(channel)
    original = Message(
        10,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 1, tzinfo=UTC),
        message="原文",
    )
    edited = Message(
        10,
        PeerChannel(123),
        date=original.date,
        message="编辑内容",
        edit_date=datetime(2026, 8, 29, 2, tzinfo=UTC),
    )

    assert asyncio.run(collector.ingest_message(original, chat_id)) is True
    assert asyncio.run(collector.ingest_message(edited, chat_id)) is True
    assert asyncio.run(collector.ingest_message(original, -100999)) is False
    assert [item.text for item in db.get_message_versions(chat_id, 10)] == [
        "原文",
        "编辑内容",
    ]

    empty_edit = Message(
        10,
        PeerChannel(123),
        date=original.date,
        message="",
        edit_date=datetime(2026, 8, 29, 3, tzinfo=UTC),
    )
    assert asyncio.run(collector.ingest_message(empty_edit, chat_id)) is True
    assert db.get_message(chat_id, 10).text == ""
    assert db.list_messages(
        chat_id,
        datetime(2026, 8, 29, tzinfo=UTC),
        datetime(2026, 8, 30, tzinfo=UTC),
    ) == []

    assert collector.ingest_deleted(chat_id, [10]) == 1
    assert db.get_message(chat_id, 10) is None


def test_resolving_allowlist_disables_persisted_sources_removed_from_config(tmp_path):
    channel = Channel(
        id=123,
        title="当前频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=-100999, title="已移除频道", username="removed", kind="channel")
    collector = TelegramCollector(
        client=FakeTelegramClient(channel, []),
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    asyncio.run(collector.resolve_sources())

    current_id = utils.get_peer_id(channel)
    assert [chat.chat_id for chat in db.list_chats()] == [current_id]
    assert db.get_chat(-100999).enabled is False


def test_resolving_allowlist_replaces_stale_in_memory_sources(tmp_path):
    channel = Channel(
        id=123,
        title="当前频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=PartiallyFailingTelegramClient(channel, []),
        database=db,
        allowlist=("@broken", "@working"),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    collector.sources[-100999] = Source(
        -100999, "旧来源", "stale", "channel", "stale-entity"
    )

    resolved = asyncio.run(collector.resolve_sources())

    assert set(collector.sources) == {utils.get_peer_id(channel)}
    assert [source.chat_id for source in resolved] == [utils.get_peer_id(channel)]


def test_reconcile_pulls_only_messages_after_persisted_cursor(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    messages = [
        Message(4, PeerChannel(123), date=datetime(2026, 8, 29, 4, tzinfo=UTC), message="补拉4"),
        Message(3, PeerChannel(123), date=datetime(2026, 8, 29, 3, tzinfo=UTC), message="补拉3"),
        Message(2, PeerChannel(123), date=datetime(2026, 8, 29, 2, tzinfo=UTC), message="已保存"),
    ]
    client = FakeTelegramClient(channel, messages)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())
    chat_id = utils.get_peer_id(channel)
    db.save_message(
        MessageRecord(chat_id, 2, datetime(2026, 8, 29, 2, tzinfo=UTC), None, "已保存")
    )

    count = asyncio.run(
        collector.reconcile(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    assert count == 2
    assert client.iter_requests[-1][1] == {"min_id": 2, "reverse": True}
    assert client.get_requests[-1][1] == (2, 3, 4)
    assert db.get_message(chat_id, 3).text == "补拉3"
    assert db.get_message(chat_id, 4).text == "补拉4"


def test_reconcile_audits_recent_edits_below_cursor_and_skips_expired_higher_ids(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    edited = Message(
        1,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 1, tzinfo=UTC),
        message="离线编辑",
        edit_date=datetime(2026, 8, 29, 4, tzinfo=UTC),
    )
    expired_higher_id = Message(
        11,
        PeerChannel(123),
        date=datetime(2026, 8, 1, 1, tzinfo=UTC),
        message="过期高 ID",
    )
    client = FakeTelegramClient(channel, [expired_higher_id, edited])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())
    chat_id = utils.get_peer_id(channel)
    db.save_messages(
        [
            MessageRecord(chat_id, 1, datetime(2026, 8, 29, 1, tzinfo=UTC), None, "原文"),
            MessageRecord(chat_id, 10, datetime(2026, 8, 29, 2, tzinfo=UTC), None, "游标"),
        ]
    )

    asyncio.run(
        collector.reconcile(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    assert db.get_message(chat_id, 1).text == "离线编辑"
    assert db.get_message(chat_id, 11) is None


def test_reconcile_audits_entire_retention_window_not_only_recent_limit(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    newest = Message(
        10,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 10, tzinfo=UTC),
        message="游标",
    )
    edited_earlier = Message(
        1,
        PeerChannel(123),
        date=datetime(2026, 8, 29, 1, tzinfo=UTC),
        message="离线编辑",
        edit_date=datetime(2026, 8, 29, 11, tzinfo=UTC),
    )
    client = FakeTelegramClient(channel, [newest, edited_earlier])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())
    chat_id = utils.get_peer_id(channel)
    db.save_messages(
        [
            MessageRecord(chat_id, 1, edited_earlier.date, None, "原文"),
            MessageRecord(chat_id, 10, newest.date, None, "游标"),
        ]
    )

    asyncio.run(
        collector.reconcile(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    assert db.get_message(chat_id, 1).text == "离线编辑"
    assert client.get_requests[-1][1] == (1, 10)


def test_reconcile_stops_forward_cursor_scan_at_retention_cutoff(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    messages = [
        Message(11, PeerChannel(123), date=datetime(2026, 8, 29, 1, tzinfo=UTC), message="保留"),
        Message(12, PeerChannel(123), date=datetime(2026, 8, 1, 1, tzinfo=UTC), message="截止"),
        Message(13, PeerChannel(123), date=datetime(2026, 7, 1, 1, tzinfo=UTC), message="不应扫描"),
    ]
    client = FakeTelegramClient(channel, messages)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())
    chat_id = utils.get_peer_id(channel)
    db.save_message(MessageRecord(chat_id, 10, datetime(2026, 8, 29, tzinfo=UTC), None, "游标"))

    asyncio.run(
        collector.reconcile(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    assert db.get_message(chat_id, 11) is not None
    assert db.get_message(chat_id, 13) is None


def test_source_resolution_failure_does_not_block_later_allowlisted_source(tmp_path):
    channel = Channel(
        id=123,
        title="可用频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="working",
    )
    client = PartiallyFailingTelegramClient(channel, [])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@broken", "@working"),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    resolved = asyncio.run(collector.resolve_sources())

    assert [source.username for source in resolved] == ["working"]
    assert client.requested == ["@broken", "@working"]
    assert db.status_counts()["source_errors"][0]["reference"] == "@broken"


def test_invalid_entity_does_not_block_later_allowlisted_source(tmp_path):
    channel = Channel(
        id=123,
        title="可用频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="working",
    )
    client = InvalidEntityTelegramClient(channel, [])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@invalid", "@working"),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    resolved = asyncio.run(collector.resolve_sources())

    assert [source.username for source in resolved] == ["working"]


def test_backfill_failure_for_one_source_does_not_block_other_source(tmp_path):
    channel = Channel(
        id=123,
        title="可用频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="working",
    )
    message = Message(
        1, PeerChannel(123), date=datetime(2026, 8, 29, 1, tzinfo=UTC), message="可用消息"
    )
    client = PerEntityTelegramClient(channel, [message])
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@broken", "@working"),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    working_id = utils.get_peer_id(channel)
    collector.sources = {
        -100999: Source(-100999, "坏来源", "broken", "channel", "broken-entity"),
        working_id: Source(working_id, "可用频道", "working", "channel", channel),
    }
    for source in collector.sources.values():
        db.upsert_chat(
            chat_id=source.chat_id,
            title=source.title,
            username=source.username,
            kind=source.kind,
        )

    count = asyncio.run(
        collector.backfill(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    assert count == 1
    assert db.get_message(working_id, 1).text == "可用消息"
    assert db.get_sync_state(-100999).last_error == "backfill failed"


def test_reconcile_without_cursor_does_not_scan_or_save_expired_history(tmp_path):
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    messages = [
        Message(2, PeerChannel(123), date=datetime(2026, 8, 29, 2, tzinfo=UTC), message="保留"),
        Message(1, PeerChannel(123), date=datetime(2026, 8, 1, 2, tzinfo=UTC), message="过期"),
    ]
    client = FakeTelegramClient(channel, messages)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )
    asyncio.run(collector.resolve_sources())

    count = asyncio.run(
        collector.reconcile(
            now=datetime(2026, 8, 29, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
    )

    chat_id = utils.get_peer_id(channel)
    assert count == 1
    assert client.iter_requests[-1][1] == {}
    assert db.get_message(chat_id, 2).text == "保留"
    assert db.get_message(chat_id, 1) is None


def test_run_forever_periodically_reconciles_messages_missed_during_disconnect(tmp_path):
    now = datetime.now(UTC)
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    first = Message(
        1, PeerChannel(123), date=now - timedelta(hours=2), message="首次回填"
    )
    missed = Message(
        2, PeerChannel(123), date=now - timedelta(hours=1), message="断线缺失"
    )
    client = DisconnectingTelegramClient(channel, [first], missed)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
        reconcile_interval_seconds=0.005,
    )

    asyncio.run(collector.run_forever())

    chat_id = utils.get_peer_id(channel)
    assert db.get_message(chat_id, 1).text == "首次回填"
    assert db.get_message(chat_id, 2).text == "断线缺失"


def test_run_forever_performs_final_reconcile_when_client_returns(tmp_path):
    now = datetime.now(UTC)
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    first = Message(
        1, PeerChannel(123), date=now - timedelta(hours=2), message="首次回填"
    )
    missed = Message(
        2, PeerChannel(123), date=now - timedelta(hours=1), message="退出前缺失"
    )
    client = DisconnectingTelegramClient(channel, [first], missed)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
        reconcile_interval_seconds=3600,
    )

    asyncio.run(collector.run_forever())

    assert db.get_message(utils.get_peer_id(channel), 2).text == "退出前缺失"


def test_run_forever_skips_final_reconcile_after_client_is_disconnected(tmp_path):
    now = datetime.now(UTC)
    channel = Channel(
        id=123,
        title="真实频道",
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 8, 1, tzinfo=UTC),
        broadcast=True,
        username="hezu1",
    )
    first = Message(
        1, PeerChannel(123), date=now - timedelta(hours=2), message="首次回填"
    )
    missed = Message(
        2, PeerChannel(123), date=now - timedelta(hours=1), message="退出时缺失"
    )
    client = OfflineAfterDisconnectClient(channel, [first], missed)
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    collector = TelegramCollector(
        client=client,
        database=db,
        allowlist=("@hezu1",),
        retention_days=6,
        timezone=ZoneInfo("Asia/Shanghai"),
    )

    asyncio.run(collector.run_forever())

    assert db.get_message(utils.get_peer_id(channel), 1).text == "首次回填"
    assert db.get_message(utils.get_peer_id(channel), 2) is None
