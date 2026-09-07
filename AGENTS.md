# Telegram Message Collector

## Project overview

This is a Python 3.11+ Telegram MTProto collector built with Telethon, SQLite, and an OpenAI-compatible summarization API. It collects only explicitly allowlisted sources, stores retained messages locally, and summarizes each source independently.

Development and real-service acceptance happen locally first. Do not upload or deploy to Ubuntu until the user explicitly approves deployment.

## Source of truth

- Product requirements: `docs/requirements.md`
- Local acceptance procedure: `docs/local-testing.md`
- Ubuntu draft: `docs/ubuntu-deployment-draft.md`
- Work history and verified results: `progress.md`
- Runtime configuration: `config.yaml`
- Example configuration: `config.example.yaml`

Read the relevant files before changing behavior. Update requirements and tests when a requested behavior changes.

## Environment and setup

- Use `uv`; do not install project packages globally.
- Install development dependencies with `uv sync --dev`.
- Supported Python versions are defined in `pyproject.toml`.
- The project does not automatically load `.env`.
- Never read, print, expose, modify, or commit real secrets unless the user explicitly requests a narrowly scoped local action.
- Treat Telegram session files as login credentials.

## Commands

```bash
uv run tg-collector --config config.yaml init-db
uv run tg-collector --config config.yaml status
uv run tg-collector --config config.yaml login
uv run tg-collector --config config.yaml sources list
uv run tg-collector --config config.yaml sources validate
uv run tg-collector --config config.yaml backfill
uv run tg-collector --config config.yaml run
uv run tg-collector --config config.yaml summarize
uv run tg-collector --config config.yaml scheduler
uv run tg-collector --config config.yaml cleanup --dry-run
uv run tg-collector --config config.yaml backup --destination backups/telegram.sqlite3
```

Use the project virtual environment directly only when `uv` is unavailable:

```bash
.venv/bin/tg-collector --config config.yaml <command>
```

## Required verification

For every production-code behavior change:

1. Add or update a regression test first.
2. Run the focused test and confirm it fails for the expected reason.
3. Implement the smallest correct change.
4. Run the focused test again.
5. Run all gates before reporting completion:

```bash
uv run pytest tests -q
uvx ruff check src tests
uvx mypy src/tg_collector --ignore-missing-imports --no-error-summary
uv run python -m compileall -q src tests
uv build
```

For database changes, also run `PRAGMA integrity_check` against the relevant test database. Do not claim real Telegram or AI acceptance without actual tool output.

## Telegram safety and collection rules

- Collect only sources listed in `telegram.allowlist`.
- Never automatically join a group or channel.
- Never send, edit, react to, forward, or delete Telegram messages.
- Accept only a valid `@username`, stable numeric `chat_id`, or supported single-segment public HTTP(S) Telegram URL.
- Reject invite links, message links, reserved paths, query strings, fragments, credentials, ports, and non-HTTP schemes.
- Resolve source equivalence through Telegram and use the stable `chat_id` internally.
- Store aware UTC timestamps with microsecond precision.
- Identify messages by `(chat_id, message_id)` and preserve distinct edit versions.
- Reconcile retained message IDs to recover offline edits and deletions.

## Normal sources

For sources present only in `telegram.allowlist`:

- Save retained text messages and required metadata.
- Save a Telegram message link when one can be generated.
- Preserve sender identity fields required by the normal message record.
- Summarize each source independently.

## Special URL and bio sources

Every entry in `telegram.shop_url_channels` must also exist in `telegram.allowlist`. This list must never expand the base allowlist.

For these sources:

- Extract explicit HTTP/HTTPS URLs and valid bare-domain URLs from message text.
- Normalize bare domains to HTTPS.
- Do not treat decimal prices such as `0.85` or `11.30` as domains.
- Deduplicate URLs per message and replace the URL set after an edit.
- Save only the currently visible sender bio when Telegram provides one.
- Do not save sender ID, username, name, phone number, avatar, or profile history.
- Do not create a user/profile table.
- Do not save the Telegram message link.
- Channel-identity and anonymous-admin messages may have no user bio; do not call the user-profile API for channel senders.
- A profile-read failure must not block message and URL persistence.

## Retention and summaries

- Business timezone: `Asia/Shanghai`.
- Default summary times: 08:00, 12:00, and 22:00.
- Summary windows are half-open and must not overlap.
- Retain today plus the previous five calendar days.
- Cleanup runs weekly according to configuration.
- Expired raw messages and their summaries are deleted even if summarization failed.
- AI errors must never extend retention.
- Summary work must remain isolated from collection and cleanup.
- Preserve the persistent summary ownership, lease, publishing, and irreversible deleting state-machine invariants already covered by tests.

## Database and file safety

- SQLite runs in WAL mode on local storage.
- Use short, batched transactions.
- Advance synchronization state only after message persistence succeeds.
- Never place SQLite on NFS.
- Backups must reject the live database and its WAL/SHM/journal aliases.
- Validate summary directories and parent components before replacement or deletion.
- Never follow a summary-file symlink to delete its target.
- Journal summary publication and deletion so interrupted operations are recoverable.
- Do not run destructive cleanup acceptance without a current verified backup and a dry run.

## Sensitive files

Never commit, upload, or expose:

- `.env`
- Telegram API hash, login codes, or 2FA passwords
- AI API keys
- Telegram `.session` files
- `data/` databases and summaries
- backups containing messages
- logs or exports containing private message text or bios

Keep credential and data files owner-only. Do not print private message text or bio values during aggregate acceptance checks.

## Configuration roles

- `config.yaml` is the active main configuration.
- `config.ayan-acceptance.yaml` and `config.qiuqiuai1919-acceptance.yaml` are isolated real-source acceptance configurations.
- Do not treat an acceptance configuration as a production source list.
- Do not merge a tested source into the main configuration unless the user explicitly requests it.

## Deployment gate

- Local code, real Telegram, and real AI acceptance must precede server deployment.
- Do not upload code, credentials, sessions, databases, or backups to Ubuntu without explicit approval.
- Do not create or modify remote services, systemd units, firewall rules, Tailscale settings, or scheduled jobs without explicit approval.
- Keep credentials local and out of chat.

## Repository discipline

- This directory may not be a Git repository; verify before using Git commands.
- Do not initialize Git, commit, push, or rewrite history unless explicitly requested.
- Make narrowly scoped changes and avoid unrelated refactors or formatting.
- Reference code locations as `path:line` in reports.
- Update `progress.md` after verified project changes.
