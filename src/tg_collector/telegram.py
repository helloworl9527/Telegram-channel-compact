from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telethon import events, functions, utils
from telethon.tl.types import Channel, Chat

from .schedule import retention_cutoff
from .storage import Database, MessageRecord

logger = logging.getLogger(__name__)
_URL_PATTERN = re.compile(
    r"(?:https?://[^\s<>\"'，。；：！？、]+|(?<![@\w])(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:/[^\s<>\"'，。；：！？、]*)?)",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、)]}》】'\""
_TELEGRAM_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")
_BARE_DOMAIN_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Source:
    chat_id: int
    title: str
    username: str | None
    kind: str
    entity: Any


def normalize_source_reference(reference: str) -> str | int:
    value = reference.strip()
    if not value:
        raise ValueError("empty Telegram source reference")
    if value.lstrip("-").isdigit():
        return int(value)
    if value.startswith("@"):
        username = value[1:]
        if _TELEGRAM_USERNAME.fullmatch(username) is None:
            raise ValueError(f"invalid Telegram username: {value!r}")
        return f"@{username}"
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Telegram source links must use HTTP or HTTPS")
    if (
        parsed.hostname is not None
        and parsed.hostname.lower()
        in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
    ):
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Telegram source links must not contain query or credentials")
        try:
            if parsed.port is not None:
                raise ValueError("Telegram source links must not contain a port")
        except ValueError as exc:
            if "port" in str(exc):
                raise
            raise ValueError("invalid Telegram source link") from exc
        segments = parsed.path.strip("/").split("/") if parsed.path.strip("/") else []
        if len(segments) != 1:
            raise ValueError("Telegram source links must contain only a username")
        segment = segments[0]
        if (
            not segment
            or segment.startswith("+")
            or segment.lower() in {"joinchat", "c", "s"}
            or _TELEGRAM_USERNAME.fullmatch(segment) is None
        ):
            raise ValueError("private invite links are not supported in the allowlist")
        return f"@{segment}"
    raise ValueError(f"unsupported Telegram source reference: {value!r}")


def source_references_may_match(left: str | int, right: str | int) -> bool:
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return True


def extract_shop_urls(text: str) -> tuple[str, ...]:
    """Extract explicit HTTP URLs and domain-shaped links in message order."""
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        if not candidate:
            continue
        normalized = candidate if "://" in candidate else f"https://{candidate}"
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if "://" not in candidate and (
            parsed.hostname is None
            or _BARE_DOMAIN_HOST.fullmatch(parsed.hostname) is None
        ):
            continue
        if normalized not in urls:
            urls.append(normalized)
    return tuple(urls)


def message_link_url(source: Source, message_id: int) -> str | None:
    if source.username:
        return f"https://t.me/{source.username}/{message_id}"
    if source.chat_id <= -1_000_000_000_000:
        internal_id = abs(source.chat_id) - 1_000_000_000_000
        return f"https://t.me/c/{internal_id}/{message_id}"
    return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def message_to_record(
    message: Any,
    source: Source,
    *,
    collect_shop_urls: bool = False,
    include_sender_id: bool = True,
) -> MessageRecord | None:
    text = str(getattr(message, "message", "") or "")
    if not text.strip() and getattr(message, "edit_date", None) is None:
        return None
    sent_at = getattr(message, "date", None)
    if sent_at is None:
        raise ValueError(f"message {message.id} has no date")
    source_url = message_link_url(source, int(message.id))
    return MessageRecord(
        chat_id=source.chat_id,
        message_id=int(message.id),
        sent_at=_aware_utc(sent_at),
        sender_id=getattr(message, "sender_id", None) if include_sender_id else None,
        text=text,
        edited_at=(
            _aware_utc(message.edit_date)
            if getattr(message, "edit_date", None) is not None
            else None
        ),
        reply_to_id=getattr(message, "reply_to_msg_id", None),
        views=getattr(message, "views", None),
        forwards=getattr(message, "forwards", None),
        has_media=getattr(message, "media", None) is not None,
        source_url=source_url,
        shop_urls=extract_shop_urls(text) if collect_shop_urls else (),
    )


class TelegramCollector:
    def __init__(
        self,
        *,
        client: Any,
        database: Database,
        allowlist: tuple[str, ...],
        retention_days: int,
        timezone: ZoneInfo,
        shop_url_channels: tuple[str, ...] = (),
        batch_size: int = 500,
        reconcile_interval_seconds: float = 300.0,
    ):
        if reconcile_interval_seconds <= 0:
            raise ValueError("reconcile interval must be positive")
        self.client = client
        self.database = database
        self.allowlist = allowlist
        self.shop_url_channels = shop_url_channels
        self.retention_days = retention_days
        self.timezone = timezone
        self.batch_size = batch_size
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.sources: dict[int, Source] = {}
        self.shop_url_chat_ids: set[int] = set()
        self._bio_cache: dict[int, str | None] = {}

        allowlist_refs = {normalize_source_reference(item) for item in allowlist}
        invalid_shop_refs = [
            item
            for item in shop_url_channels
            if not any(
                source_references_may_match(normalize_source_reference(item), allowed)
                for allowed in allowlist_refs
            )
        ]
        if invalid_shop_refs:
            raise ValueError(
                "shop_url_channels must also be present in Telegram allowlist: "
                + ", ".join(invalid_shop_refs)
            )

    async def resolve_sources(self) -> list[Source]:
        resolved: list[Source] = []
        self.sources = {}
        self.shop_url_chat_ids = set()

        for configured in self.allowlist:
            try:
                reference = normalize_source_reference(configured)
                entity = await self.client.get_entity(reference)
            except Exception:
                logger.exception("failed to resolve allowlisted source %r", configured)
                self.database.set_source_error(configured, "source resolution failed")
                continue
            if isinstance(entity, Channel):
                kind = "group" if entity.megagroup else "channel"
            elif isinstance(entity, Chat):
                kind = "group"
            else:
                logger.error(
                    "allowlist entry is not a group or channel: %r", configured
                )
                self.database.set_source_error(
                    configured, "allowlist entry is not a group or channel"
                )
                continue
            source = Source(
                chat_id=utils.get_peer_id(entity),
                title=entity.title,
                username=getattr(entity, "username", None),
                kind=kind,
                entity=entity,
            )
            self.database.upsert_chat(
                chat_id=source.chat_id,
                title=source.title,
                username=source.username,
                kind=source.kind,
            )
            self.sources[source.chat_id] = source
            self.database.set_source_error(configured, None)
            if any(
                self._matches_shop_reference(item, source)
                for item in self.shop_url_channels
            ):
                self.shop_url_chat_ids.add(source.chat_id)
            resolved.append(source)
        for configured in self.shop_url_channels:
            error_key = f"shop_url_channel:{configured}"
            if any(
                self._matches_shop_reference(configured, source)
                for source in resolved
            ):
                self.database.set_source_error(error_key, None)
            else:
                self.database.set_source_error(
                    error_key, "special source did not match the resolved allowlist"
                )
        self.database.set_enabled_chats({source.chat_id for source in resolved})
        return resolved

    @staticmethod
    def _matches_shop_reference(reference: str, source: Source) -> bool:
        normalized = normalize_source_reference(reference)
        if isinstance(normalized, int):
            return normalized == source.chat_id
        return normalized.casefold() == f"@{source.username or ''}".casefold()

    def retention_cutoff(self, now: datetime) -> datetime:
        return retention_cutoff(
            now, retention_days=self.retention_days, tz=self.timezone
        )

    async def _message_record(
        self, message: Any, source: Source
    ) -> MessageRecord | None:
        is_shop_source = source.chat_id in self.shop_url_chat_ids
        record = message_to_record(
            message,
            source,
            collect_shop_urls=is_shop_source,
            include_sender_id=not is_shop_source,
        )
        if record is None or not is_shop_source:
            return record
        return replace(
            record,
            source_url=None,
            sender_bio=await self._sender_bio(message),
        )

    async def _sender_bio(self, message: Any) -> str | None:
        sender_id = getattr(message, "sender_id", None)
        if sender_id is None:
            return None
        cache_key = int(sender_id)
        if cache_key in self._bio_cache:
            return self._bio_cache[cache_key]
        try:
            entity = await self.client.get_entity(sender_id)
            if isinstance(entity, (Channel, Chat)):
                self._bio_cache[cache_key] = None
                return None
            value = getattr(entity, "about", None)
            if not value:
                full = await self.client(functions.users.GetFullUserRequest(entity))
                value = getattr(getattr(full, "full_user", None), "about", None)
            bio = str(value).strip() if value else None
        except Exception:
            logger.warning("failed to read Telegram sender bio", exc_info=True)
            bio = None
        if bio is not None:
            self._bio_cache[cache_key] = bio
        return bio

    async def backfill(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        cutoff = self.retention_cutoff(current)
        sources = list(self.sources.values()) or await self.resolve_sources()
        total = 0
        for source in sources:
            try:
                batch: list[MessageRecord] = []
                async for message in self.client.iter_messages(source.entity):
                    message_date = getattr(message, "date", None)
                    if message_date is None:
                        continue
                    if _aware_utc(message_date) < cutoff:
                        break
                    record = await self._message_record(message, source)
                    if record is None:
                        continue
                    batch.append(record)
                    if len(batch) >= self.batch_size:
                        total += self.database.save_messages(batch)
                        batch = []
                if batch:
                    total += self.database.save_messages(batch)
            except Exception:
                logger.exception("failed to backfill source %s", source.chat_id)
                self.database.set_sync_error(source.chat_id, "backfill failed")
            else:
                self.database.set_sync_error(source.chat_id, None)
        return total

    async def reconcile(self, *, now: datetime | None = None) -> int:
        """Pull new messages and audit recent messages for offline edits."""
        cutoff = self.retention_cutoff(now or datetime.now(UTC))
        sources = list(self.sources.values()) or await self.resolve_sources()
        total = 0
        for source in sources:
            try:
                state = self.database.get_sync_state(source.chat_id)
                iterator_options = (
                    {"min_id": state.last_message_id, "reverse": True} if state else {}
                )
                batch: list[MessageRecord] = []
                async for message in self.client.iter_messages(
                    source.entity, **iterator_options
                ):
                    message_date = getattr(message, "date", None)
                    if message_date is None:
                        continue
                    if _aware_utc(message_date) < cutoff:
                        break
                    record = await self._message_record(message, source)
                    if record is None:
                        continue
                    batch.append(record)
                    if len(batch) >= self.batch_size:
                        total += self.database.save_messages(batch)
                        batch = []
                if batch:
                    total += self.database.save_messages(batch)

                if state is not None:
                    audit_batch: list[MessageRecord] = []
                    retained_ids = self.database.list_retained_message_ids(
                        source.chat_id, cutoff
                    )
                    for offset in range(0, len(retained_ids), self.batch_size):
                        requested_ids = retained_ids[offset : offset + self.batch_size]
                        messages = await self.client.get_messages(
                            source.entity, ids=requested_ids
                        )
                        returned_ids: set[int] = set()
                        for message in messages:
                            if message is None:
                                continue
                            returned_ids.add(int(message.id))
                            record = await self._message_record(message, source)
                            if record is not None:
                                audit_batch.append(record)
                        missing_ids = [
                            message_id
                            for message_id in requested_ids
                            if message_id not in returned_ids
                        ]
                        if missing_ids:
                            self.database.delete_messages(source.chat_id, missing_ids)
                        if audit_batch:
                            self.database.save_messages(audit_batch)
                            audit_batch = []
            except Exception:
                logger.exception("failed to reconcile source %s", source.chat_id)
                self.database.set_sync_error(source.chat_id, "reconciliation failed")
            else:
                self.database.set_sync_error(source.chat_id, None)
        return total

    async def ingest_message(self, message: Any, chat_id: int) -> bool:
        source = self.sources.get(chat_id)
        if source is None:
            return False
        record = await self._message_record(message, source)
        if record is None:
            return False
        self.database.save_message(record)
        return True

    def ingest_deleted(self, chat_id: int, message_ids: list[int]) -> int:
        if chat_id not in self.sources:
            return 0
        return self.database.delete_messages(chat_id, message_ids)

    async def run_forever(self) -> None:
        await self.resolve_sources()
        chat_ids = tuple(self.sources)

        async def handle_message_event(event: Any) -> None:
            await self.ingest_message(event.message, event.chat_id)

        async def handle_deleted_event(event: Any) -> None:
            if event.chat_id is not None:
                self.ingest_deleted(event.chat_id, list(event.deleted_ids))

        self.client.add_event_handler(
            handle_message_event, events.NewMessage(chats=chat_ids)
        )
        self.client.add_event_handler(
            handle_message_event, events.MessageEdited(chats=chat_ids)
        )
        self.client.add_event_handler(
            handle_deleted_event, events.MessageDeleted(chats=chat_ids)
        )
        await self.backfill()
        reconciliation = asyncio.create_task(self._reconcile_periodically())
        try:
            await self.client.run_until_disconnected()
        finally:
            reconciliation.cancel()
            with suppress(asyncio.CancelledError):
                await reconciliation
            is_connected = getattr(self.client, "is_connected", None)
            if is_connected is None or is_connected():
                try:
                    await self.reconcile()
                except Exception:
                    logger.exception("final Telegram reconciliation failed")

    async def _reconcile_periodically(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval_seconds)
            try:
                await self.reconcile()
            except Exception:
                logger.exception("periodic Telegram reconciliation failed")
