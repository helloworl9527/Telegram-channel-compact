from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_required_utc(value: str) -> datetime:
    parsed = _parse_utc(value)
    if parsed is None:
        raise ValueError("required UTC timestamp is missing")
    return parsed


def _utc_microseconds(value: str | None) -> int | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _safe_child_path(root: Path, value: str | Path) -> Path:
    candidate = Path(os.path.abspath(os.path.expanduser(str(value))))
    canonical_parent = candidate.parent.resolve()
    if not canonical_parent.is_relative_to(root):
        raise ValueError("summary path is outside configured summary directory")
    return canonical_parent / candidate.name


@dataclass(frozen=True, slots=True)
class MessageRecord:
    chat_id: int
    message_id: int
    sent_at: datetime
    sender_id: int | None
    text: str
    edited_at: datetime | None = None
    reply_to_id: int | None = None
    views: int | None = None
    forwards: int | None = None
    has_media: bool = False
    source_url: str | None = None
    sender_bio: str | None = None
    shop_urls: tuple[str, ...] = ()

    @property
    def message_url(self) -> str | None:
        return self.source_url


@dataclass(frozen=True, slots=True)
class MessageVersion:
    chat_id: int
    message_id: int
    text: str
    edited_at: datetime | None
    collected_at: datetime


@dataclass(frozen=True, slots=True)
class CleanupResult:
    deleted_messages: int
    deleted_summaries: int


@dataclass(frozen=True, slots=True)
class SyncState:
    chat_id: int
    last_message_id: int
    last_synced_at: datetime
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRecord:
    chat_id: int
    title: str
    username: str | None
    kind: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SummaryJobRecord:
    job_id: int
    status: str
    input_hash: str
    attempts: int


@dataclass(frozen=True, slots=True)
class SummaryJobPreparation:
    job_id: int
    stale_output_path: Path | None


@dataclass(frozen=True, slots=True)
class StoredSummary:
    job_id: int
    content: str
    output_path: Path


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.create_function("utc_us", 1, _utc_microseconds, deterministic=True)
        self._secure_database_files()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-journal"),
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(0o600)

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            self._secure_database_files()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    kind TEXT NOT NULL CHECK (kind IN ('channel', 'group')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    sender_id INTEGER,
                    text TEXT NOT NULL,
                    edited_at TEXT,
                    reply_to_id INTEGER,
                    views INTEGER,
                    forwards INTEGER,
                    has_media INTEGER NOT NULL DEFAULT 0,
                    source_url TEXT,
                    sender_bio TEXT,
                    collected_at TEXT NOT NULL,
                    deleted_at TEXT,
                    PRIMARY KEY (chat_id, message_id),
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_sent
                    ON messages(chat_id, sent_at DESC, message_id DESC);

                CREATE TABLE IF NOT EXISTS message_urls (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, message_id, url),
                    FOREIGN KEY (chat_id, message_id)
                        REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS message_versions (
                    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    text TEXT NOT NULL,
                    edited_at TEXT,
                    collected_at TEXT NOT NULL,
                    UNIQUE (chat_id, message_id, content_hash),
                    FOREIGN KEY (chat_id, message_id)
                        REFERENCES messages(chat_id, message_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    chat_id INTEGER PRIMARY KEY,
                    last_message_id INTEGER NOT NULL,
                    last_synced_at TEXT NOT NULL,
                    last_error TEXT,
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS source_errors (
                    reference TEXT PRIMARY KEY,
                    error TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS summary_jobs (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_revision TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    claim_token TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, window_start, window_end),
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS summaries (
                    summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    content TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES summary_jobs(job_id) ON DELETE CASCADE,
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS summary_request_cache (
                    job_id INTEGER NOT NULL,
                    request_hash TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, request_hash),
                    FOREIGN KEY (job_id) REFERENCES summary_jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS cleanup_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cutoff TEXT NOT NULL,
                    deleted_messages INTEGER NOT NULL,
                    deleted_summaries INTEGER NOT NULL,
                    started_at TEXT,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_summary_deletions (
                    job_id INTEGER PRIMARY KEY,
                    output_path TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES summary_jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS pending_orphan_summary_deletions (
                    job_id INTEGER NOT NULL,
                    output_path TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'delete',
                    PRIMARY KEY (job_id, output_path, reason)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(cleanup_runs)")
            }
            if "started_at" not in columns:
                connection.execute("ALTER TABLE cleanup_runs ADD COLUMN started_at TEXT")
            message_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "sender_bio" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN sender_bio TEXT")
            summary_job_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(summary_jobs)")
            }
            if "claim_token" not in summary_job_columns:
                connection.execute("ALTER TABLE summary_jobs ADD COLUMN claim_token TEXT")
            if "snapshot_revision" not in summary_job_columns:
                connection.execute(
                    """
                    ALTER TABLE summary_jobs
                    ADD COLUMN snapshot_revision TEXT NOT NULL DEFAULT ''
                    """
                )
            pending_orphan_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(pending_orphan_summary_deletions)"
                )
            }
            if "reason" not in pending_orphan_columns:
                connection.execute(
                    """
                    ALTER TABLE pending_orphan_summary_deletions
                    ADD COLUMN reason TEXT NOT NULL DEFAULT 'delete'
                    """
                )
            orphan_pk = tuple(
                row["name"]
                for row in sorted(
                    connection.execute(
                        "PRAGMA table_info(pending_orphan_summary_deletions)"
                    ).fetchall(),
                    key=lambda row: row["pk"],
                )
                if row["pk"]
            )
            if orphan_pk != ("job_id", "output_path", "reason"):
                connection.executescript(
                    """
                    CREATE TABLE pending_orphan_summary_deletions_new (
                        job_id INTEGER NOT NULL,
                        output_path TEXT NOT NULL,
                        window_end TEXT NOT NULL,
                        queued_at TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT 'delete',
                        PRIMARY KEY (job_id, output_path, reason)
                    );
                    INSERT OR IGNORE INTO pending_orphan_summary_deletions_new(
                        job_id, output_path, window_end, queued_at, reason
                    )
                    SELECT job_id, output_path, window_end, queued_at, reason
                    FROM pending_orphan_summary_deletions;
                    DROP TABLE pending_orphan_summary_deletions;
                    ALTER TABLE pending_orphan_summary_deletions_new
                        RENAME TO pending_orphan_summary_deletions;
                    """
                )
            self._migrate_summary_job_identity(connection)

    @staticmethod
    def _migrate_summary_job_identity(connection: sqlite3.Connection) -> None:
        unique_indexes = [
            row
            for row in connection.execute("PRAGMA index_list(summary_jobs)")
            if row["unique"]
        ]
        for index in unique_indexes:
            columns = tuple(
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info('{index['name']}')"
                )
            )
            if columns == ("chat_id", "window_start", "window_end"):
                return

        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TEMP TABLE summary_job_keep AS
                SELECT job_id FROM summary_jobs AS candidate
                WHERE candidate.job_id = (
                    SELECT selected.job_id
                    FROM summary_jobs AS selected
                    WHERE selected.chat_id = candidate.chat_id
                      AND selected.window_start = candidate.window_start
                      AND selected.window_end = candidate.window_end
                    ORDER BY utc_us(selected.updated_at) DESC, selected.job_id DESC
                    LIMIT 1
                );

                INSERT OR IGNORE INTO pending_orphan_summary_deletions(
                    job_id, output_path, window_end, queued_at, reason
                )
                SELECT discarded.job_id, discarded.output_path,
                       discarded.window_end,
                       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'delete'
                FROM summaries AS discarded
                WHERE discarded.job_id NOT IN (
                    SELECT job_id FROM summary_job_keep
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM summaries AS kept
                    WHERE kept.job_id IN (SELECT job_id FROM summary_job_keep)
                      AND kept.output_path = discarded.output_path
                  );

                DELETE FROM summaries
                WHERE job_id NOT IN (SELECT job_id FROM summary_job_keep);
                DELETE FROM summary_request_cache
                WHERE job_id NOT IN (SELECT job_id FROM summary_job_keep);
                DELETE FROM pending_summary_deletions
                WHERE job_id NOT IN (SELECT job_id FROM summary_job_keep);

                CREATE TABLE summary_jobs_new (
                    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    snapshot_revision TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    claim_token TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, window_start, window_end),
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );
                INSERT INTO summary_jobs_new(
                    job_id, chat_id, window_start, window_end, input_hash,
                    snapshot_revision, model, prompt_version,
                    status, attempts, error, claim_token,
                    created_at, updated_at
                )
                SELECT job_id, chat_id, window_start, window_end, input_hash,
                       snapshot_revision, model, prompt_version,
                       status, attempts, error, claim_token,
                       created_at, updated_at
                FROM summary_jobs
                WHERE job_id IN (SELECT job_id FROM summary_job_keep);
                DROP TABLE summary_jobs;
                ALTER TABLE summary_jobs_new RENAME TO summary_jobs;
                DROP TABLE summary_job_keep;
                COMMIT;
                """
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination).expanduser().resolve()
        source_path = self.path.expanduser().resolve()
        protected_paths = {
            source_path,
            Path(f"{source_path}-wal"),
            Path(f"{source_path}-shm"),
            Path(f"{source_path}-journal"),
        }
        same_inode = any(
            target.exists()
            and protected.exists()
            and os.path.samefile(target, protected)
            for protected in protected_paths
        )
        if target in protected_paths or same_inode:
            raise ValueError("backup destination must not be in the same database family")
        parent_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            target.parent.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self._connect() as source, sqlite3.connect(temporary) as output:
                source.backup(output)
            temporary.chmod(0o600)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def upsert_chat(
        self,
        *,
        chat_id: int,
        title: str,
        username: str | None,
        kind: str,
        enabled: bool = True,
    ) -> None:
        if kind not in {"channel", "group"}:
            raise ValueError(f"unsupported chat kind: {kind}")
        now = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chats(chat_id, title, username, kind, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    kind = excluded.kind,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, username, kind, int(enabled), now),
            )

    def get_chat(self, chat_id: int) -> ChatRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        return ChatRecord(
            chat_id=row["chat_id"],
            title=row["title"],
            username=row["username"],
            kind=row["kind"],
            enabled=bool(row["enabled"]),
        )

    def list_chats(self, *, enabled_only: bool = True) -> list[ChatRecord]:
        query = "SELECT * FROM chats"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY chat_id"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [
            ChatRecord(
                chat_id=row["chat_id"],
                title=row["title"],
                username=row["username"],
                kind=row["kind"],
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def set_enabled_chats(self, enabled_chat_ids: set[int]) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE chats SET enabled = 0")
            if enabled_chat_ids:
                placeholders = ",".join("?" for _ in enabled_chat_ids)
                connection.execute(
                    f"UPDATE chats SET enabled = 1 WHERE chat_id IN ({placeholders})",
                    tuple(enabled_chat_ids),
                )

    def save_message(self, message: MessageRecord) -> None:
        self.save_messages([message])

    def save_messages(self, messages: list[MessageRecord]) -> int:
        if not messages:
            return 0
        collected_at = _utc_text(datetime.now(UTC))
        assert collected_at is not None
        max_ids: dict[int, int] = {}
        with self._connect() as connection:
            for message in messages:
                self._save_message_row(connection, message, collected_at)
                max_ids[message.chat_id] = max(
                    message.message_id, max_ids.get(message.chat_id, message.message_id)
                )
            for chat_id, last_message_id in max_ids.items():
                connection.execute(
                    """
                    INSERT INTO sync_state(chat_id, last_message_id, last_synced_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        last_message_id = MAX(sync_state.last_message_id, excluded.last_message_id),
                        last_synced_at = excluded.last_synced_at,
                        last_error = NULL
                    """,
                    (chat_id, last_message_id, collected_at),
                )
        return len(messages)

    @staticmethod
    def _save_message_row(
        connection: sqlite3.Connection,
        message: MessageRecord,
        collected_at: str,
    ) -> None:
        sent_at = _utc_text(message.sent_at)
        edited_at = _utc_text(message.edited_at)
        content_hash = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
        message_write = connection.execute(
            """
            INSERT INTO messages(
                chat_id, message_id, sent_at, sender_id, text, edited_at,
                reply_to_id, views, forwards, has_media, source_url, sender_bio,
                collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                sender_id = excluded.sender_id,
                text = excluded.text,
                edited_at = excluded.edited_at,
                reply_to_id = excluded.reply_to_id,
                views = excluded.views,
                forwards = excluded.forwards,
                has_media = excluded.has_media,
                source_url = excluded.source_url,
                sender_bio = excluded.sender_bio,
                collected_at = excluded.collected_at,
                deleted_at = NULL
            WHERE utc_us(COALESCE(excluded.edited_at, excluded.sent_at))
                >= utc_us(COALESCE(messages.edited_at, messages.sent_at))
            """,
            (
                message.chat_id,
                message.message_id,
                sent_at,
                message.sender_id,
                message.text,
                edited_at,
                message.reply_to_id,
                message.views,
                message.forwards,
                int(message.has_media),
                message.source_url,
                message.sender_bio,
                collected_at,
            ),
        )
        if message_write.rowcount == 1:
            connection.execute(
                "DELETE FROM message_urls WHERE chat_id = ? AND message_id = ?",
                (message.chat_id, message.message_id),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO message_urls(chat_id, message_id, url, collected_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (message.chat_id, message.message_id, url, collected_at)
                    for url in dict.fromkeys(message.shop_urls)
                    if url.strip()
                ],
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO message_versions(
                chat_id, message_id, content_hash, text, edited_at, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message.chat_id,
                message.message_id,
                content_hash,
                message.text,
                edited_at,
                collected_at,
            ),
        )

    def get_message(self, chat_id: int, message_id: int) -> MessageRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return self._row_to_message(row) if row else None

    def get_message_urls(self, chat_id: int, message_id: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT url FROM message_urls
                WHERE chat_id = ? AND message_id = ?
                ORDER BY rowid
                """,
                (chat_id, message_id),
            ).fetchall()
        return [str(row["url"]) for row in rows]

    def list_messages(
        self,
        chat_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> list[MessageRecord]:
        start_text = _utc_text(window_start)
        end_text = _utc_text(window_end)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND utc_us(sent_at) >= utc_us(?)
                    AND utc_us(sent_at) < utc_us(?)
                    AND deleted_at IS NULL
                    AND TRIM(text) <> ''
                ORDER BY sent_at, message_id
                """,
                (chat_id, start_text, end_text),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_retained_message_ids(self, chat_id: int, cutoff: datetime) -> list[int]:
        cutoff_text = _utc_text(cutoff)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE chat_id = ? AND utc_us(sent_at) >= utc_us(?)
                ORDER BY message_id
                """,
                (chat_id, cutoff_text),
            ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def get_sync_state(self, chat_id: int) -> SyncState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_state WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            return None
        return SyncState(
            chat_id=row["chat_id"],
            last_message_id=row["last_message_id"],
            last_synced_at=_parse_required_utc(row["last_synced_at"]),
            last_error=row["last_error"],
        )

    def set_sync_error(self, chat_id: int, error: str | None) -> None:
        now = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_state(
                    chat_id, last_message_id, last_synced_at, last_error
                ) VALUES (?, 0, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET last_error = excluded.last_error
                """,
                (chat_id, now, error),
            )

    def set_source_error(self, reference: str, error: str | None) -> None:
        with self._connect() as connection:
            if error is None:
                connection.execute(
                    "DELETE FROM source_errors WHERE reference = ?", (reference,)
                )
            else:
                connection.execute(
                    """
                    INSERT INTO source_errors(reference, error, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(reference) DO UPDATE SET
                        error = excluded.error,
                        updated_at = excluded.updated_at
                    """,
                    (reference, error, _utc_text(datetime.now(UTC))),
                )

    def delete_messages(self, chat_id: int, message_ids: list[int]) -> int:
        if not message_ids:
            return 0
        placeholders = ",".join("?" for _ in message_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM messages WHERE chat_id = ? AND message_id IN ({placeholders})",
                (chat_id, *message_ids),
            )
        return cursor.rowcount

    def get_message_versions(self, chat_id: int, message_id: int) -> list[MessageVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chat_id, message_id, text, edited_at, collected_at
                FROM message_versions
                WHERE chat_id = ? AND message_id = ?
                ORDER BY version_id
                """,
                (chat_id, message_id),
            ).fetchall()
        return [
            MessageVersion(
                chat_id=row["chat_id"],
                message_id=row["message_id"],
                text=row["text"],
                edited_at=_parse_utc(row["edited_at"]),
                collected_at=_parse_required_utc(row["collected_at"]),
            )
            for row in rows
        ]

    def create_summary_job(
        self,
        *,
        chat_id: int,
        window_start: datetime,
        window_end: datetime,
        input_hash: str,
        model: str,
        prompt_version: str,
        snapshot_revision: str = "",
    ) -> SummaryJobPreparation:
        start_text = _utc_text(window_start)
        end_text = _utc_text(window_end)
        now = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            previous = connection.execute(
                """
                SELECT j.job_id, j.input_hash, j.snapshot_revision, j.status,
                       j.model, j.prompt_version,
                       s.output_path
                FROM summary_jobs AS j
                LEFT JOIN summaries AS s ON s.job_id = j.job_id
                WHERE j.chat_id = ? AND j.window_start = ? AND j.window_end = ?
                """,
                (chat_id, start_text, end_text),
            ).fetchone()
            if previous is not None and previous["status"] == "deleting":
                return SummaryJobPreparation(int(previous["job_id"]), None)
            if (
                previous is not None
                and snapshot_revision
                and previous["snapshot_revision"]
                and snapshot_revision < previous["snapshot_revision"]
            ):
                return SummaryJobPreparation(int(previous["job_id"]), None)
            input_changed = previous is not None and (
                previous["input_hash"] != input_hash
                or previous["snapshot_revision"] != snapshot_revision
                or previous["model"] != model
                or previous["prompt_version"] != prompt_version
            )
            stale_output_path = (
                Path(previous["output_path"])
                if input_changed and previous["output_path"]
                else None
            )
            connection.execute(
                """
                INSERT INTO summary_jobs(
                    chat_id, window_start, window_end, input_hash,
                    snapshot_revision, model, prompt_version,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(chat_id, window_start, window_end)
                DO UPDATE SET
                    input_hash = excluded.input_hash,
                    snapshot_revision = excluded.snapshot_revision,
                    model = excluded.model,
                    prompt_version = excluded.prompt_version,
                    status = CASE
                        WHEN summary_jobs.input_hash = excluded.input_hash
                            AND summary_jobs.snapshot_revision = excluded.snapshot_revision
                            AND summary_jobs.model = excluded.model
                            AND summary_jobs.prompt_version = excluded.prompt_version
                        THEN summary_jobs.status ELSE 'pending' END,
                    attempts = CASE
                        WHEN summary_jobs.input_hash = excluded.input_hash
                            AND summary_jobs.snapshot_revision = excluded.snapshot_revision
                            AND summary_jobs.model = excluded.model
                            AND summary_jobs.prompt_version = excluded.prompt_version
                        THEN summary_jobs.attempts ELSE 0 END,
                    error = CASE
                        WHEN summary_jobs.input_hash = excluded.input_hash
                            AND summary_jobs.snapshot_revision = excluded.snapshot_revision
                            AND summary_jobs.model = excluded.model
                            AND summary_jobs.prompt_version = excluded.prompt_version
                        THEN summary_jobs.error ELSE NULL END,
                    claim_token = CASE
                        WHEN summary_jobs.input_hash = excluded.input_hash
                            AND summary_jobs.snapshot_revision = excluded.snapshot_revision
                            AND summary_jobs.model = excluded.model
                            AND summary_jobs.prompt_version = excluded.prompt_version
                        THEN summary_jobs.claim_token ELSE NULL END,
                    updated_at = CASE
                        WHEN summary_jobs.input_hash = excluded.input_hash
                            AND summary_jobs.snapshot_revision = excluded.snapshot_revision
                            AND summary_jobs.model = excluded.model
                            AND summary_jobs.prompt_version = excluded.prompt_version
                        THEN summary_jobs.updated_at ELSE excluded.updated_at END
                """,
                (
                    chat_id,
                    start_text,
                    end_text,
                    input_hash,
                    snapshot_revision,
                    model,
                    prompt_version,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT job_id FROM summary_jobs
                WHERE chat_id = ? AND window_start = ? AND window_end = ?
                """,
                (chat_id, start_text, end_text),
            ).fetchone()
            job_id = int(row["job_id"])
            if input_changed:
                if stale_output_path is not None:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO pending_orphan_summary_deletions(
                            job_id, output_path, window_end, queued_at, reason
                        ) VALUES (?, ?, ?, ?, 'delete')
                        """,
                        (job_id, str(stale_output_path), end_text, now),
                    )
                connection.execute("DELETE FROM summaries WHERE job_id = ?", (job_id,))
                connection.execute(
                    "DELETE FROM summary_request_cache WHERE job_id = ?", (job_id,)
                )
            if stale_output_path is None:
                pending = connection.execute(
                    """
                    SELECT output_path FROM pending_orphan_summary_deletions
                    WHERE job_id = ? AND reason = 'delete'
                    """,
                    (job_id,),
                ).fetchone()
                if pending is not None:
                    stale_output_path = Path(pending["output_path"])
        return SummaryJobPreparation(job_id, stale_output_path)

    def complete_summary_job(
        self,
        job_id: int,
        *,
        claim_token: str,
        content: str,
        output_path: str | Path,
    ) -> bool:
        now = _utc_text(datetime.now(UTC))
        path_text = str(Path(output_path))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """
                SELECT * FROM summary_jobs
                WHERE job_id = ? AND status = 'running' AND claim_token = ?
                """,
                (job_id, claim_token),
            ).fetchone()
            if job is None:
                return False
            connection.execute(
                """
                INSERT INTO summaries(
                    job_id, chat_id, window_start, window_end,
                    content, output_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content = excluded.content,
                    output_path = excluded.output_path,
                    created_at = excluded.created_at
                """,
                (
                    job_id,
                    job["chat_id"],
                    job["window_start"],
                    job["window_end"],
                    content,
                    path_text,
                    now,
                ),
            )
            updated = connection.execute(
                """
                UPDATE summary_jobs
                SET status = 'completed', error = NULL, claim_token = NULL, updated_at = ?
                WHERE job_id = ? AND claim_token = ?
                """,
                (now, job_id, claim_token),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False
        return True

    def publish_summary_job(
        self,
        job_id: int,
        *,
        claim_token: str,
        content: str,
        temporary_path: str | Path,
        output_path: str | Path,
        output_root: str | Path,
    ) -> bool:
        """Publish through a durable two-phase owner protocol."""
        now = _utc_text(datetime.now(UTC))
        temporary = Path(temporary_path)
        target = Path(output_path)
        root = Path(output_root).expanduser().resolve()
        safe_temporary = _safe_child_path(root, temporary)
        safe_target = _safe_child_path(root, target)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """
                SELECT window_end FROM summary_jobs
                WHERE job_id = ? AND status = 'running' AND claim_token = ?
                """,
                (job_id, claim_token),
            ).fetchone()
            if job is None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT OR REPLACE INTO pending_orphan_summary_deletions(
                    job_id, output_path, window_end, queued_at, reason
                ) VALUES (?, ?, ?, ?, 'publish')
                """,
                (job_id, str(safe_target), job["window_end"], now),
            )
            updated = connection.execute(
                """
                UPDATE summary_jobs SET status = 'publishing', updated_at = ?
                WHERE job_id = ? AND status = 'running' AND claim_token = ?
                """,
                (now, job_id, claim_token),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return False

        replaced = False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = connection.execute(
                    """
                    SELECT * FROM summary_jobs
                    WHERE job_id = ? AND status = 'publishing' AND claim_token = ?
                    """,
                    (job_id, claim_token),
                ).fetchone()
                if job is None:
                    connection.rollback()
                    return False
                os.replace(safe_temporary, safe_target)
                replaced = True
                connection.execute(
                    """
                    INSERT INTO summaries(
                        job_id, chat_id, window_start, window_end,
                        content, output_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        content = excluded.content,
                        output_path = excluded.output_path,
                        created_at = excluded.created_at
                    """,
                    (
                        job_id,
                        job["chat_id"],
                        job["window_start"],
                        job["window_end"],
                        content,
                        str(safe_target),
                        now,
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE summary_jobs
                    SET status = 'completed', error = NULL,
                        claim_token = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'publishing' AND claim_token = ?
                    """,
                    (now, job_id, claim_token),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("summary ownership changed during publish")
                connection.execute(
                    """
                    DELETE FROM pending_orphan_summary_deletions
                    WHERE job_id = ? AND output_path = ? AND reason = 'publish'
                    """,
                    (job_id, str(safe_target)),
                )
        except Exception:
            if replaced:
                safe_target.unlink(missing_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE summary_jobs
                    SET status = 'failed', attempts = attempts + 1,
                        error = 'summary publish failed', claim_token = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'publishing' AND claim_token = ?
                    """,
                    (_utc_text(datetime.now(UTC)), job_id, claim_token),
                )
                if not safe_target.exists():
                    connection.execute(
                        """
                        DELETE FROM pending_orphan_summary_deletions
                        WHERE job_id = ? AND output_path = ? AND reason = 'publish'
                        """,
                        (job_id, str(safe_target)),
                    )
            raise
        return True

    def invalidate_summary_job(self, job_id: int) -> Path | None:
        now = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stored = connection.execute(
                """
                SELECT output_path, window_end FROM summaries WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if stored is not None:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO pending_orphan_summary_deletions(
                        job_id, output_path, window_end, queued_at, reason
                    ) VALUES (?, ?, ?, ?, 'delete')
                    """,
                    (job_id, stored["output_path"], stored["window_end"], now),
                )
            connection.execute(
                "DELETE FROM summaries WHERE job_id = ?", (job_id,)
            )
            connection.execute(
                "DELETE FROM summary_request_cache WHERE job_id = ?", (job_id,)
            )
            connection.execute(
                """
                UPDATE summary_jobs
                SET status = 'pending', attempts = 0, error = NULL,
                    claim_token = NULL, updated_at = updated_at
                WHERE job_id = ? AND status = 'completed'
                """,
                (job_id,),
            )
        return Path(stored["output_path"]) if stored is not None else None

    def acknowledge_pending_summary_file(
        self, job_id: int, output_path: str | Path
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM pending_orphan_summary_deletions
                WHERE job_id = ? AND output_path = ? AND reason = 'delete'
                """,
                (job_id, str(Path(output_path))),
            )

    def get_summary_job(self, job_id: int) -> SummaryJobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, status, input_hash, attempts FROM summary_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return SummaryJobRecord(
            job_id=row["job_id"],
            status=row["status"],
            input_hash=row["input_hash"],
            attempts=row["attempts"],
        )

    def claim_summary_job(
        self,
        job_id: int,
        *,
        input_hash: str,
        model: str,
        prompt_version: str,
        snapshot_revision: str = "",
        lease_seconds: int = 900,
    ) -> str | None:
        if lease_seconds < 0:
            raise ValueError("summary lease must not be negative")
        now_value = datetime.now(UTC)
        now = _utc_text(now_value)
        stale_before = _utc_text(now_value - timedelta(seconds=lease_seconds))
        claim_token = uuid4().hex
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE summary_jobs
                SET status = 'running', claim_token = ?, updated_at = ?
                WHERE job_id = ?
                  AND input_hash = ?
                  AND snapshot_revision = ?
                  AND model = ?
                  AND prompt_version = ?
                  AND (
                    status IN ('pending', 'failed')
                    OR (
                        status IN ('running', 'publishing')
                        AND utc_us(updated_at) < utc_us(?)
                    )
                  )
                """,
                (
                    claim_token,
                    now,
                    job_id,
                    input_hash,
                    snapshot_revision,
                    model,
                    prompt_version,
                    stale_before,
                ),
            )
        return claim_token if cursor.rowcount == 1 else None

    def renew_summary_job(self, job_id: int, claim_token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE summary_jobs SET updated_at = ?
                WHERE job_id = ? AND status = 'running' AND claim_token = ?
                """,
                (_utc_text(datetime.now(UTC)), job_id, claim_token),
            )
        return cursor.rowcount == 1

    def get_summary(self, job_id: int) -> StoredSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, content, output_path FROM summaries WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredSummary(
            job_id=row["job_id"],
            content=row["content"],
            output_path=Path(row["output_path"]),
        )

    def get_cached_summary_request(self, job_id: int, request_hash: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content FROM summary_request_cache
                WHERE job_id = ? AND request_hash = ?
                """,
                (job_id, request_hash),
            ).fetchone()
        return str(row["content"]) if row else None

    def cache_summary_request(
        self, job_id: int, claim_token: str, request_hash: str, content: str
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO summary_request_cache(
                    job_id, request_hash, content, created_at
                )
                SELECT ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM summary_jobs
                    WHERE job_id = ? AND status = 'running' AND claim_token = ?
                )
                """,
                (
                    job_id,
                    request_hash,
                    content,
                    _utc_text(datetime.now(UTC)),
                    job_id,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1

    def fail_summary_job(self, job_id: int, claim_token: str, error: str) -> bool:
        now = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE summary_jobs
                SET status = 'failed', attempts = attempts + 1,
                    error = ?, claim_token = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND claim_token = ?
                """,
                (error, now, job_id, claim_token),
            )
        return cursor.rowcount == 1

    def status_counts(self) -> dict[str, object]:
        with self._connect() as connection:
            resolved_sources = int(
                connection.execute("SELECT COUNT(*) FROM chats WHERE enabled = 1").fetchone()[0]
            )
            messages = int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            grouped = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM summary_jobs GROUP BY status"
                ).fetchall()
            }
            sources = [
                {
                    "chat_id": row["chat_id"],
                    "title": row["title"],
                    "username": row["username"],
                    "kind": row["kind"],
                    "last_message_id": row["last_message_id"],
                    "last_synced_at": row["last_synced_at"],
                    "last_error": row["last_error"],
                }
                for row in connection.execute(
                    """
                    SELECT c.chat_id, c.title, c.username, c.kind,
                           s.last_message_id, s.last_synced_at, s.last_error
                    FROM chats AS c
                    LEFT JOIN sync_state AS s ON s.chat_id = c.chat_id
                    WHERE c.enabled = 1
                    ORDER BY c.chat_id
                    """
                ).fetchall()
            ]
            cleanup_row = connection.execute(
                """
                SELECT cutoff, deleted_messages, deleted_summaries, started_at, completed_at
                FROM cleanup_runs ORDER BY run_id DESC LIMIT 1
                """
            ).fetchone()
            latest_summaries = connection.execute(
                """
                SELECT s.chat_id, c.title, s.window_start, s.window_end, s.created_at
                FROM summaries AS s
                JOIN chats AS c ON c.chat_id = s.chat_id
                WHERE s.summary_id IN (
                    SELECT MAX(summary_id) FROM summaries GROUP BY chat_id
                )
                ORDER BY s.chat_id
                """
            ).fetchall()
            source_errors = connection.execute(
                "SELECT reference, error, updated_at FROM source_errors ORDER BY reference"
            ).fetchall()
        return {
            "resolved_sources": resolved_sources,
            "messages": messages,
            "summary_jobs": {
                status: grouped.get(status, 0)
                for status in ("pending", "running", "failed", "completed")
            },
            "sources": sources,
            "source_errors": [dict(row) for row in source_errors],
            "latest_successful_summaries": [dict(row) for row in latest_summaries],
            "last_cleanup": dict(cleanup_row) if cleanup_row else None,
        }

    def preview_cleanup(self, cutoff: datetime) -> CleanupResult:
        cutoff_text = _utc_text(cutoff)
        with self._connect() as connection:
            deleted_messages = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE utc_us(sent_at) < utc_us(?)",
                    (cutoff_text,),
                ).fetchone()[0]
            )
            deleted_summaries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM summaries WHERE utc_us(window_end) <= utc_us(?)",
                    (cutoff_text,),
                ).fetchone()[0]
            )
        return CleanupResult(deleted_messages, deleted_summaries)

    def cleanup_expired(
        self, cutoff: datetime, *, summary_dir: str | Path
    ) -> CleanupResult:
        cutoff_text = _utc_text(cutoff)
        started_at = _utc_text(datetime.now(UTC))
        summary_root = Path(summary_dir).expanduser().resolve()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO pending_summary_deletions(
                    job_id, output_path, window_end, queued_at
                )
                SELECT job_id, output_path, window_end, ?
                FROM summaries
                WHERE utc_us(window_end) <= utc_us(?)
                """,
                (started_at, cutoff_text),
            )
            connection.execute(
                """
                UPDATE summary_jobs
                SET status = 'deleting', claim_token = NULL, updated_at = ?
                WHERE utc_us(window_end) <= utc_us(?)
                  AND status <> 'deleting'
                """,
                (started_at, cutoff_text),
            )
            pending_rows = connection.execute(
                """
                SELECT job_id, output_path
                FROM pending_summary_deletions
                WHERE utc_us(window_end) <= utc_us(?)
                UNION ALL
                SELECT job_id, output_path
                FROM pending_orphan_summary_deletions
                WHERE utc_us(window_end) <= utc_us(?)
                ORDER BY job_id
                """,
                (cutoff_text, cutoff_text),
            ).fetchall()
            output_paths = [
                _safe_child_path(summary_root, row["output_path"])
                for row in pending_rows
            ]
            deleted_summaries = int(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM summaries
                         WHERE utc_us(window_end) <= utc_us(?))
                        +
                        (SELECT COUNT(*) FROM pending_orphan_summary_deletions
                         WHERE utc_us(window_end) <= utc_us(?))
                    """,
                    (cutoff_text, cutoff_text),
                ).fetchone()[0]
            )
            deleted_messages = int(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE utc_us(sent_at) < utc_us(?)",
                    (cutoff_text,),
                ).fetchone()[0]
            )

        for output_path in output_paths:
            output_path.unlink(missing_ok=True)

        completed_at = _utc_text(datetime.now(UTC))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE utc_us(sent_at) < utc_us(?)",
                (cutoff_text,),
            )
            connection.execute(
                "DELETE FROM summary_jobs WHERE utc_us(window_end) <= utc_us(?)",
                (cutoff_text,),
            )
            connection.execute(
                """
                DELETE FROM pending_orphan_summary_deletions
                WHERE utc_us(window_end) <= utc_us(?)
                """,
                (cutoff_text,),
            )
            connection.execute(
                """
                INSERT INTO cleanup_runs(
                    cutoff, deleted_messages, deleted_summaries, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    cutoff_text,
                    deleted_messages,
                    deleted_summaries,
                    started_at,
                    completed_at,
                ),
            )
        return CleanupResult(deleted_messages, deleted_summaries)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            sent_at=_parse_required_utc(row["sent_at"]),
            sender_id=row["sender_id"],
            text=row["text"],
            edited_at=_parse_utc(row["edited_at"]),
            reply_to_id=row["reply_to_id"],
            views=row["views"],
            forwards=row["forwards"],
            has_media=bool(row["has_media"]),
            source_url=row["source_url"],
            sender_bio=row["sender_bio"],
        )
