import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tg_collector.storage import Database, MessageRecord


def _claim(
    db,
    job_id,
    *,
    input_hash="hash",
    model="model",
    prompt_version="v1",
    snapshot_revision="",
    lease_seconds=900,
):
    return db.claim_summary_job(
        job_id,
        input_hash=input_hash,
        model=model,
        prompt_version=prompt_version,
        snapshot_revision=snapshot_revision,
        lease_seconds=lease_seconds,
    )


def test_message_edit_updates_latest_and_keeps_distinct_versions(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")

    original = MessageRecord(
        chat_id=1001,
        message_id=42,
        sent_at=datetime(2026, 8, 29, 1, 0, tzinfo=UTC),
        sender_id=7,
        text="初始内容",
    )
    edited = MessageRecord(
        chat_id=1001,
        message_id=42,
        sent_at=original.sent_at,
        sender_id=7,
        text="编辑后的内容",
        edited_at=datetime(2026, 8, 29, 1, 30, tzinfo=UTC),
    )

    db.save_message(original)
    db.save_message(original)
    db.save_message(edited)

    assert db.get_message(1001, 42).text == "编辑后的内容"
    assert [version.text for version in db.get_message_versions(1001, 42)] == [
        "初始内容",
        "编辑后的内容",
    ]


def test_older_edit_one_microsecond_apart_cannot_overwrite_newer_edit(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    sent_at = datetime(2026, 8, 29, 1, tzinfo=UTC)
    newer = MessageRecord(
        1001,
        42,
        sent_at,
        7,
        "较新编辑",
        edited_at=sent_at + timedelta(microseconds=2),
    )
    older = MessageRecord(
        1001,
        42,
        sent_at,
        7,
        "较旧编辑",
        edited_at=sent_at + timedelta(microseconds=1),
    )

    db.save_message(newer)
    db.save_message(older)

    assert db.get_message(1001, 42).text == "较新编辑"


def test_message_keeps_link_urls_and_current_bio_without_user_profile_table(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    message = MessageRecord(
        1001,
        42,
        datetime(2026, 8, 29, 1, tzinfo=UTC),
        7,
        "店铺 shop.example/item",
        source_url="https://t.me/channel/42",
        sender_bio="店铺简介",
        shop_urls=("shop.example/item", "https://shop.example/item"),
    )

    db.save_message(message)

    stored = db.get_message(1001, 42)
    assert stored.message_url == "https://t.me/channel/42"
    assert stored.sender_bio == "店铺简介"
    assert db.get_message_urls(1001, 42) == [
        "shop.example/item",
        "https://shop.example/item",
    ]
    with sqlite3.connect(db.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "users" not in tables


def test_message_edit_replaces_removed_shop_urls_and_bio(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    base = MessageRecord(
        1001,
        42,
        datetime(2026, 8, 29, 1, tzinfo=UTC),
        None,
        "旧 shop.example/old",
        sender_bio="旧简介",
        shop_urls=("shop.example/old",),
    )
    edited = MessageRecord(
        1001,
        42,
        base.sent_at,
        None,
        "新 other.example/new",
        edited_at=datetime(2026, 8, 29, 2, tzinfo=UTC),
        sender_bio="新简介",
        shop_urls=("other.example/new",),
    )

    db.save_message(base)
    db.save_message(edited)

    assert db.get_message_urls(1001, 42) == ["other.example/new"]
    assert db.get_message(1001, 42).sender_bio == "新简介"


def test_stale_edit_does_not_replace_current_shop_urls(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    sent_at = datetime(2026, 8, 29, 1, tzinfo=UTC)
    current = MessageRecord(
        1001,
        42,
        sent_at,
        None,
        "新链接",
        edited_at=sent_at + timedelta(microseconds=2),
        shop_urls=("https://new.example",),
    )
    stale = MessageRecord(
        1001,
        42,
        sent_at,
        None,
        "旧链接",
        edited_at=sent_at + timedelta(microseconds=1),
        shop_urls=("https://old.example",),
    )

    db.save_message(current)
    db.save_message(stale)

    assert db.get_message_urls(1001, 42) == ["https://new.example"]


def test_cleanup_force_deletes_expired_messages_and_matching_summary(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    old_sent_at = cutoff - timedelta(hours=1)
    current_sent_at = cutoff + timedelta(hours=1)
    db.save_message(MessageRecord(1001, 1, old_sent_at, 7, "过期但未总结"))
    db.save_message(MessageRecord(1001, 2, current_sent_at, 7, "仍在保留期"))

    summary_path = tmp_path / "summaries" / "old.md"
    summary_path.parent.mkdir()
    summary_path.write_text("旧总结", encoding="utf-8")
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=10),
        window_end=cutoff,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    claim_token = _claim(db, job_id)
    assert claim_token is not None
    assert db.complete_summary_job(
        job_id, claim_token=claim_token, content="旧总结", output_path=summary_path
    )

    result = db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert result.deleted_messages == 1
    assert result.deleted_summaries == 1
    assert db.get_message(1001, 1) is None
    assert db.get_message_versions(1001, 1) == []
    assert db.get_message(1001, 2) is not None
    assert not summary_path.exists()
    cleanup = db.status_counts()["last_cleanup"]
    assert cleanup["started_at"] <= cleanup["completed_at"]


def test_batch_write_advances_cursor_and_window_query_is_half_open(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, 0, tzinfo=UTC)
    inside = MessageRecord(1001, 1, start, 7, "窗口内")
    boundary = MessageRecord(1001, 2, end, 7, "下个窗口")

    db.save_messages([inside, boundary])

    assert [message.message_id for message in db.list_messages(1001, start, end)] == [1]
    assert db.get_sync_state(1001).last_message_id == 2


def test_cleanup_preview_counts_without_deleting(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    db.save_message(
        MessageRecord(1001, 1, cutoff - timedelta(seconds=1), 7, "即将删除")
    )

    result = db.preview_cleanup(cutoff)

    assert result.deleted_messages == 1
    assert result.deleted_summaries == 0
    assert db.get_message(1001, 1) is not None


def test_database_file_is_owner_only(tmp_path):
    path = tmp_path / "private" / "collector.sqlite3"

    Database(path).initialize()

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_backup_refuses_hard_link_to_active_database(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    alias = tmp_path / "hard-link.sqlite3"
    os.link(db.path, alias)

    with pytest.raises(ValueError, match="same database family"):
        db.backup(alias)

    with sqlite3.connect(db.path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_backup_refuses_database_sidecar_destination(tmp_path, suffix):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    sidecar = db.path.with_name(db.path.name + suffix)

    with pytest.raises(ValueError, match="same database family"):
        db.backup(sidecar)


def test_backup_does_not_change_existing_parent_directory_permissions(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    parent = tmp_path / "shared-backups"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    db.backup(parent / "backup.sqlite3")

    assert parent.stat().st_mode & 0o777 == 0o755


def test_cleanup_refuses_summary_path_outside_configured_directory(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    victim = tmp_path / "must-not-delete.txt"
    victim.write_text("private", encoding="utf-8")
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=1),
        window_end=cutoff,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    claim_token = _claim(db, job_id)
    assert claim_token is not None
    assert db.complete_summary_job(
        job_id, claim_token=claim_token, content="总结", output_path=victim
    )

    with pytest.raises(ValueError, match="outside configured summary directory"):
        db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert victim.exists()
    assert db.get_summary(job_id) is not None


def test_cleanup_unlinks_summary_symlink_without_deleting_target(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    target = summary_dir / "still-valid.md"
    target.write_text("仍有效", encoding="utf-8")
    link = summary_dir / "expired.md"
    link.symlink_to(target)
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=1),
        window_end=cutoff,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    claim_token = _claim(db, job_id)
    assert claim_token is not None
    assert db.complete_summary_job(
        job_id, claim_token=claim_token, content="总结", output_path=link
    )

    db.cleanup_expired(cutoff, summary_dir=summary_dir)

    assert target.exists()
    assert not link.exists()


def test_cleanup_accepts_macos_var_private_var_alias_without_following_final_symlink(
    tmp_path,
):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    resolved_root = str(summary_dir.resolve())
    if not resolved_root.startswith("/private/var/"):
        pytest.skip("macOS /var alias is not present")
    target = summary_dir / "target.md"
    target.write_text("保留", encoding="utf-8")
    real_link = summary_dir / "expired.md"
    real_link.symlink_to(target)
    alias_link = Path(str(real_link).replace("/private/var/", "/var/", 1))
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=1),
        window_end=cutoff,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    claim_token = _claim(db, job_id)
    assert claim_token is not None
    assert db.complete_summary_job(
        job_id, claim_token=claim_token, content="总结", output_path=alias_link
    )

    db.cleanup_expired(cutoff, summary_dir=summary_dir)

    assert target.exists()
    assert not real_link.exists()


def test_cleanup_does_not_expire_fractional_timestamp_after_exact_cutoff(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    message = MessageRecord(
        1001,
        1,
        cutoff + timedelta(microseconds=500_000),
        7,
        "截止点之后",
    )
    db.save_message(message)

    result = db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert result.deleted_messages == 0
    assert db.get_message(1001, 1) is not None


def test_cleanup_expires_message_one_microsecond_before_cutoff(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    db.save_message(
        MessageRecord(
            1001,
            1,
            cutoff - timedelta(microseconds=1),
            7,
            "截止点前一微秒",
        )
    )

    result = db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert result.deleted_messages == 1
    assert db.get_message(1001, 1) is None


def test_cleanup_compares_legacy_second_precision_timestamp_chronologically(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, 0, 0, 0, 500_000, tzinfo=UTC)
    db.save_message(MessageRecord(1001, 1, cutoff, 7, "精确截止点"))
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "UPDATE messages SET sent_at = ? WHERE chat_id = ? AND message_id = ?",
            ("2026-08-24T00:00:00Z", 1001, 1),
        )

    result = db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert result.deleted_messages == 1
    assert db.get_message(1001, 1) is None


def test_cleanup_file_failure_keeps_database_state_for_retry(tmp_path, monkeypatch):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    summary_path = tmp_path / "summaries" / "old.md"
    summary_path.parent.mkdir()
    summary_path.write_text("总结", encoding="utf-8")
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=1),
        window_end=cutoff,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    claim_token = _claim(db, job_id)
    assert claim_token is not None
    assert db.complete_summary_job(
        job_id, claim_token=claim_token, content="总结", output_path=summary_path
    )
    original_unlink = type(summary_path).unlink

    def fail_target(path, *args, **kwargs):
        if path.resolve() == summary_path.resolve():
            raise PermissionError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(summary_path), "unlink", fail_target)

    with pytest.raises(PermissionError, match="denied"):
        db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert db.get_summary(job_id) is not None
    assert db.status_counts()["last_cleanup"] is None
    with sqlite3.connect(db.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_summary_deletions WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0] == 1

    monkeypatch.setattr(type(summary_path), "unlink", original_unlink)
    result = db.cleanup_expired(cutoff, summary_dir=tmp_path / "summaries")

    assert result.deleted_summaries == 1
    assert db.get_summary(job_id) is None
    assert not summary_path.exists()


def test_expired_summary_owner_cannot_complete_or_fail_after_reclaim(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    job_id = db.create_summary_job(
        chat_id=1001,
        window_start=datetime(2026, 8, 29, tzinfo=UTC),
        window_end=datetime(2026, 8, 29, 4, tzinfo=UTC),
        input_hash="hash",
        model="model",
        prompt_version="v1",
    ).job_id
    first_owner = _claim(db, job_id)
    second_owner = _claim(db, job_id, lease_seconds=0)

    assert first_owner is not None
    assert second_owner is not None
    assert first_owner != second_owner
    assert not db.complete_summary_job(
        job_id,
        claim_token=first_owner,
        content="旧执行者结果",
        output_path=tmp_path / "stale.md",
    )
    assert not db.fail_summary_job(job_id, first_owner, "旧执行者失败")
    assert db.complete_summary_job(
        job_id,
        claim_token=second_owner,
        content="新执行者结果",
        output_path=tmp_path / "current.md",
    )
    assert db.get_summary(job_id).content == "新执行者结果"


def test_summary_job_identity_is_source_and_window_not_model_or_prompt(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)

    first = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="hash-1",
        model="model-a",
        prompt_version="v1",
    )
    second = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="hash-2",
        model="model-b",
        prompt_version="v2",
    )

    assert second.job_id == first.job_id
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM summary_jobs").fetchone()[0] == 1


def test_create_summary_job_does_not_refresh_another_owner_lease(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    job = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    )
    owner = _claim(db, job.job_id)
    with sqlite3.connect(db.path) as connection:
        before = connection.execute(
            "SELECT updated_at FROM summary_jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]

    db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="hash",
        model="model",
        prompt_version="v1",
    )

    with sqlite3.connect(db.path) as connection:
        after = connection.execute(
            "SELECT updated_at, claim_token FROM summary_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert after == (before, owner)


def test_summary_claim_rejects_stale_preparation_revision(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    first = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="snapshot-a",
        model="model-a",
        prompt_version="v1",
    )
    second = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="snapshot-b",
        model="model-b",
        prompt_version="v2",
    )

    assert db.claim_summary_job(
        first.job_id,
        input_hash="snapshot-a",
        model="model-a",
        prompt_version="v1",
    ) is None
    assert db.claim_summary_job(
        second.job_id,
        input_hash="snapshot-b",
        model="model-b",
        prompt_version="v2",
    ) is not None


def test_late_older_summary_preparation_cannot_replace_newer_revision(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    newer = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="new-hash",
        model="model",
        prompt_version="v1",
        snapshot_revision="2026-08-29T02:00:00Z|2",
    )
    owner = _claim(
        db,
        newer.job_id,
        input_hash="new-hash",
        snapshot_revision="2026-08-29T02:00:00Z|2",
    )

    older = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="old-hash",
        model="model",
        prompt_version="v1",
        snapshot_revision="2026-08-29T01:00:00Z|1",
    )

    assert older.job_id == newer.job_id
    assert _claim(
        db,
        older.job_id,
        input_hash="old-hash",
        snapshot_revision="2026-08-29T01:00:00Z|1",
    ) is None
    with sqlite3.connect(db.path) as connection:
        row = connection.execute(
            "SELECT input_hash, claim_token FROM summary_jobs WHERE job_id = ?",
            (newer.job_id,),
        ).fetchone()
    assert row == ("new-hash", owner)


def test_deleting_summary_job_cannot_be_revived_by_new_revision(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    start = datetime(2026, 8, 29, tzinfo=UTC)
    end = datetime(2026, 8, 29, 4, tzinfo=UTC)
    job = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="old",
        model="model",
        prompt_version="v1",
        snapshot_revision="2026-08-29T01:00:00Z|1",
    )
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "UPDATE summary_jobs SET status = 'deleting' WHERE job_id = ?",
            (job.job_id,),
        )

    refreshed = db.create_summary_job(
        chat_id=1001,
        window_start=start,
        window_end=end,
        input_hash="new",
        model="model-new",
        prompt_version="v2",
        snapshot_revision="2026-08-29T02:00:00Z|2",
    )

    assert refreshed.job_id == job.job_id
    assert _claim(
        db,
        job.job_id,
        input_hash="new",
        model="model-new",
        prompt_version="v2",
        snapshot_revision="2026-08-29T02:00:00Z|2",
    ) is None
    with sqlite3.connect(db.path) as connection:
        row = connection.execute(
            "SELECT status, input_hash FROM summary_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
    assert row == ("deleting", "old")


def test_stale_summary_owner_cannot_publish_markdown(tmp_path):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    db.upsert_chat(chat_id=1001, title="频道", username="channel", kind="channel")
    job = db.create_summary_job(
        chat_id=1001,
        window_start=datetime(2026, 8, 29, tzinfo=UTC),
        window_end=datetime(2026, 8, 29, 4, tzinfo=UTC),
        input_hash="hash",
        model="model",
        prompt_version="v1",
    )
    stale_owner = _claim(db, job.job_id)
    current_owner = _claim(db, job.job_id, lease_seconds=0)
    stale_temp = tmp_path / "stale.tmp"
    stale_target = tmp_path / "stale.md"
    current_temp = tmp_path / "current.tmp"
    current_target = tmp_path / "current.md"
    stale_temp.write_text("旧执行者", encoding="utf-8")
    current_temp.write_text("新执行者", encoding="utf-8")

    assert stale_owner is not None
    assert current_owner is not None
    assert not db.publish_summary_job(
        job.job_id,
        claim_token=stale_owner,
        content="旧执行者",
        temporary_path=stale_temp,
        output_path=stale_target,
        output_root=tmp_path,
    )
    assert not stale_target.exists()
    assert db.publish_summary_job(
        job.job_id,
        claim_token=current_owner,
        content="新执行者",
        temporary_path=current_temp,
        output_path=current_target,
        output_root=tmp_path,
    )
    assert current_target.read_text(encoding="utf-8") == "新执行者"
    assert db.get_summary(job.job_id).output_path == current_target


def test_cleanup_tombstone_blocks_running_expired_job_publish(tmp_path, monkeypatch):
    db = Database(tmp_path / "collector.sqlite3")
    db.initialize()
    for chat_id in (1001, 1002):
        db.upsert_chat(
            chat_id=chat_id,
            title=f"频道{chat_id}",
            username=f"channel{chat_id}",
            kind="channel",
        )
    cutoff = datetime(2026, 8, 24, tzinfo=UTC)
    running = db.create_summary_job(
        chat_id=1001,
        window_start=cutoff - timedelta(hours=2),
        window_end=cutoff,
        input_hash="running",
        model="model",
        prompt_version="v1",
    )
    running_owner = _claim(db, running.job_id, input_hash="running")
    completed = db.create_summary_job(
        chat_id=1002,
        window_start=cutoff - timedelta(hours=2),
        window_end=cutoff,
        input_hash="completed",
        model="model",
        prompt_version="v1",
    )
    completed_owner = _claim(db, completed.job_id, input_hash="completed")
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    blocking_file = summary_dir / "blocking.md"
    blocking_file.write_text("阻塞清理", encoding="utf-8")
    assert running_owner is not None
    assert completed_owner is not None
    assert db.complete_summary_job(
        completed.job_id,
        claim_token=completed_owner,
        content="完成",
        output_path=blocking_file,
    )
    entered_unlink = threading.Event()
    release_unlink = threading.Event()
    original_unlink = Path.unlink

    def blocking_unlink(path, *args, **kwargs):
        if path == blocking_file:
            entered_unlink.set()
            assert release_unlink.wait(timeout=5)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocking_unlink)
    cleanup_error: list[Exception] = []

    def run_cleanup():
        try:
            db.cleanup_expired(cutoff, summary_dir=summary_dir)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            cleanup_error.append(exc)

    thread = threading.Thread(target=run_cleanup)
    thread.start()
    assert entered_unlink.wait(timeout=5)
    temporary = summary_dir / "running.tmp"
    target = summary_dir / "running.md"
    temporary.write_text("不应发布", encoding="utf-8")

    assert not db.publish_summary_job(
        running.job_id,
        claim_token=running_owner,
        content="不应发布",
        temporary_path=temporary,
        output_path=target,
        output_root=summary_dir,
    )
    release_unlink.set()
    thread.join(timeout=5)

    assert cleanup_error == []
    assert not target.exists()


def test_initialize_migrates_old_summary_job_identity_constraint(tmp_path):
    path = tmp_path / "collector.sqlite3"
    summary_dir = tmp_path / "summaries"
    summary_dir.mkdir()
    orphan_file = summary_dir / "old.md"
    orphan_file.write_text("旧任务文件", encoding="utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                kind TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE summary_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(chat_id, window_start, window_end, model, prompt_version)
            );
            CREATE TABLE summaries (
                summary_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                content TEXT NOT NULL,
                output_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO chats VALUES (
                1001, '频道', 'channel', 'channel', 1, '2026-08-29T00:00:00Z'
            );
            INSERT INTO summary_jobs(
                chat_id, window_start, window_end, input_hash, model,
                prompt_version, created_at, updated_at
            ) VALUES
                (1001, '2026-08-29T00:00:00Z', '2026-08-29T04:00:00Z',
                 'old', 'model-a', 'v1', '2026-08-29T05:00:00Z', '2026-08-29T05:00:00Z'),
                (1001, '2026-08-29T00:00:00Z', '2026-08-29T04:00:00Z',
                 'new', 'model-b', 'v2', '2026-08-29T06:00:00Z', '2026-08-29T06:00:00Z');
            """
        )
        connection.execute(
            """
            INSERT INTO summaries(
                job_id, chat_id, window_start, window_end,
                content, output_path, created_at
            ) VALUES (1, 1001, ?, ?, '旧总结', ?, ?)
            """,
            (
                "2026-08-29T00:00:00Z",
                "2026-08-29T04:00:00Z",
                str(orphan_file),
                "2026-08-29T05:00:00Z",
            ),
        )

    Database(path).initialize()

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT input_hash, model, prompt_version FROM summary_jobs"
        ).fetchall()
        assert rows == [("new", "model-b", "v2")]
        assert connection.execute(
            "SELECT COUNT(*) FROM pending_orphan_summary_deletions"
        ).fetchone()[0] == 1

    Database(path).cleanup_expired(
        datetime(2026, 8, 30, tzinfo=UTC), summary_dir=summary_dir
    )

    assert not orphan_file.exists()
