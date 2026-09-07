from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from .storage import Database, MessageRecord

REQUIRED_HEADINGS = ("# 主题", "# 重要信息", "# 主要观点", "# 消息来源")
SYSTEM_PROMPT = """你是 Telegram 消息总结助手。只能依据输入消息总结，不得补充输入中没有的事实。
输出必须使用中文 Markdown，并严格包含以下一级标题：
# 主题
# 重要信息
# 主要观点
# 消息来源
“消息来源”必须引用消息 ID 和时间；信息不确定或存在冲突时明确说明。"""


class AIClient(Protocol):
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str: ...


class OpenAIChatClient:
    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 90,
        max_retries: int = 0,
        sdk_client: Any | None = None,
    ):
        self.model = model
        self.client = sdk_client or AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("AI response did not contain text")
        return content


class RetryLimitReached(RuntimeError):
    pass


class SummaryJobInProgress(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SummaryResult:
    job_id: int
    content: str
    output_path: Path
    cached: bool


class SummaryService:
    def __init__(
        self,
        *,
        database: Database,
        ai: AIClient,
        output_dir: str | Path,
        model: str,
        max_input_chars: int,
        prompt_version: str,
        timezone: str,
        max_attempts: int = 3,
    ):
        self.database = database
        self.ai = ai
        self.output_dir = Path(output_dir)
        self.model = model
        self.max_input_chars = max_input_chars
        self.prompt_version = prompt_version
        self.timezone = ZoneInfo(timezone)
        self.max_attempts = max_attempts

    async def summarize_window(
        self,
        chat_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> SummaryResult:
        chat = self.database.get_chat(chat_id)
        if chat is None:
            raise ValueError(f"unknown chat: {chat_id}")
        messages = self.database.list_messages(chat_id, window_start, window_end)
        if not messages:
            raise ValueError("summary window contains no messages")

        input_hash = self._input_hash(messages)
        snapshot_revision = self._snapshot_revision(messages)
        preparation = self.database.create_summary_job(
            chat_id=chat_id,
            window_start=window_start,
            window_end=window_end,
            input_hash=input_hash,
            model=self.model,
            prompt_version=self.prompt_version,
            snapshot_revision=snapshot_revision,
        )
        job_id = preparation.job_id
        if preparation.stale_output_path is not None:
            self._remove_stale_output(preparation.stale_output_path)
            self.database.acknowledge_pending_summary_file(
                job_id, preparation.stale_output_path
            )
        job = self.database.get_summary_job(job_id)
        stored = self.database.get_summary(job_id)
        if job and job.status == "completed" and job.input_hash == input_hash and stored:
            if self._stored_output_available(stored.output_path, stored.content):
                return SummaryResult(job_id, stored.content, stored.output_path, True)
            stale_path = self.database.invalidate_summary_job(job_id)
            if stale_path is not None:
                self._remove_stale_output(stale_path)
                self.database.acknowledge_pending_summary_file(job_id, stale_path)
        if job and job.status == "failed" and job.attempts >= self.max_attempts:
            raise RetryLimitReached(
                f"summary job {job_id} reached {self.max_attempts} attempts"
            )
        claim_token = self.database.claim_summary_job(
            job_id,
            input_hash=input_hash,
            model=self.model,
            prompt_version=self.prompt_version,
            snapshot_revision=snapshot_revision,
        )
        if claim_token is None:
            raise SummaryJobInProgress(f"summary job {job_id} is already running")

        try:
            prefix_template = (
                f"来源：{chat.title}\n"
                f"总结窗口：{self._local_time(window_start)} 至 {self._local_time(window_end)}\n"
                "这是第 999999 个消息块。请按固定栏目总结。\n\n"
            )
            chunk_limit = self.max_input_chars - len(SYSTEM_PROMPT) - len(prefix_template)
            if chunk_limit < 1:
                raise ValueError("ai.max_input_chars is too small for summary prompt metadata")
            chunks = self._chunks(messages, limit=chunk_limit)
            chunk_summaries = []
            for index, chunk in enumerate(chunks, start=1):
                prompt = (
                    f"来源：{chat.title}\n"
                    f"总结窗口：{self._local_time(window_start)} 至 {self._local_time(window_end)}\n"
                    f"这是第 {index} 个消息块。请按固定栏目总结。\n\n{chunk}"
                )
                chunk_summaries.append(
                    await self._complete_cached(job_id, claim_token, prompt)
                )

            final_body = await self._reduce_summaries(
                job_id, claim_token, chunk_summaries
            )
            self._validate_headings(final_body)
            content = self._render_document(
                chat.title, window_start, window_end, final_body.strip()
            )
            output_path = self._output_path(chat_id, window_start, window_end)
            if not self.database.renew_summary_job(job_id, claim_token):
                raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
            temporary_path = output_path.with_name(
                f".{output_path.name}.{claim_token}.tmp"
            )
            with temporary_path.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.chmod(0o600)
            if not self.database.renew_summary_job(job_id, claim_token):
                raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
            if not self.database.publish_summary_job(
                job_id,
                claim_token=claim_token,
                content=content,
                temporary_path=temporary_path,
                output_path=output_path,
                output_root=self.output_dir,
            ):
                raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
            output_path.chmod(0o600)
            return SummaryResult(job_id, content, output_path, False)
        except Exception as exc:
            if "temporary_path" in locals():
                temporary_path.unlink(missing_ok=True)
            self.database.fail_summary_job(job_id, claim_token, str(exc))
            raise

    async def _complete_cached(
        self, job_id: int, claim_token: str, user_prompt: str
    ) -> str:
        if len(SYSTEM_PROMPT) + len(user_prompt) > self.max_input_chars:
            raise ValueError("AI user prompt exceeds configured input limit")
        if not self.database.renew_summary_job(job_id, claim_token):
            raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
        request_hash = hashlib.sha256(
            (
                f"{self.model}\n{self.prompt_version}\n{SYSTEM_PROMPT}\n{user_prompt}"
            ).encode()
        ).hexdigest()
        cached = self.database.get_cached_summary_request(job_id, request_hash)
        if cached is not None:
            return cached
        content = await self.ai.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt
        )
        if not self.database.renew_summary_job(job_id, claim_token):
            raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
        if not self.database.cache_summary_request(
            job_id, claim_token, request_hash, content
        ):
            raise SummaryJobInProgress(f"summary job {job_id} ownership expired")
        return content

    async def _reduce_summaries(
        self, job_id: int, claim_token: str, summaries: list[str]
    ) -> str:
        current = summaries
        for level in range(20):
            if len(current) == 1:
                return current[0]
            prefix = (
                f"请合并以下分块总结（归并层级 {level}）。"
                "去除重复内容，保持消息来源引用，不得加入新事实。\n\n"
            )
            available = self.max_input_chars - len(SYSTEM_PROMPT) - len(prefix)
            if available < 1:
                raise ValueError("ai.max_input_chars is too small for merge prompt metadata")
            groups = self._pack_texts(current, available)
            current = [
                await self._complete_cached(job_id, claim_token, f"{prefix}{group}")
                for group in groups
            ]
        raise ValueError("summary merge did not converge within 20 levels")

    @staticmethod
    def _pack_texts(texts: list[str], limit: int) -> list[str]:
        separator = "\n\n--- 分块总结 ---\n\n"
        pieces = [
            text[index : index + limit]
            for text in texts
            for index in range(0, max(len(text), 1), limit)
        ]
        groups: list[str] = []
        current = ""
        for piece in pieces:
            candidate = piece if not current else f"{current}{separator}{piece}"
            if current and len(candidate) > limit:
                groups.append(current)
                current = piece
            else:
                current = candidate
        if current:
            groups.append(current)
        return groups

    def _chunks(self, messages: list[MessageRecord], *, limit: int) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for message in messages:
            line = self._format_message(message)
            pieces = [
                line[index : index + limit]
                for index in range(0, len(line), limit)
            ]
            for piece in pieces:
                extra = len(piece) + (2 if current else 0)
                if current and current_size + extra > limit:
                    chunks.append("\n\n".join(current))
                    current = []
                    current_size = 0
                current.append(piece)
                current_size += len(piece) + (2 if len(current) > 1 else 0)
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    def _format_message(self, message: MessageRecord) -> str:
        parts = [
            f"时间={self._local_time(message.sent_at)}",
            f"消息ID={message.message_id}",
        ]
        if message.sender_id is not None:
            parts.append(f"发送者={message.sender_id}")
        if message.source_url:
            parts.append(f"链接={message.source_url}")
        return f"[{' | '.join(parts)}]\n{message.text}"

    @staticmethod
    def _input_hash(messages: list[MessageRecord]) -> str:
        digest = hashlib.sha256()
        for message in messages:
            digest.update(
                (
                    f"{message.chat_id}:{message.message_id}:"
                    f"{message.edited_at or message.sent_at}:{message.text}\n"
                ).encode()
            )
        return digest.hexdigest()

    @staticmethod
    def _snapshot_revision(messages: list[MessageRecord]) -> str:
        timestamp, message_id = max(
            (message.edited_at or message.sent_at, message.message_id)
            for message in messages
        )
        return (
            f"{timestamp.astimezone(UTC).isoformat(timespec='microseconds')}"
            f"|{message_id:020d}"
        )

    def _local_time(self, value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("summary timestamps must be timezone-aware")
        return value.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")

    def _output_path(
        self,
        chat_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> Path:
        configured_root = self.output_dir.expanduser()
        if configured_root.is_symlink():
            raise ValueError("summary output path is outside configured output directory")
        configured_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = configured_root.resolve()
        chat_dir = root / str(chat_id)
        if chat_dir.is_symlink():
            raise ValueError("summary output path is outside configured output directory")
        chat_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not chat_dir.resolve().is_relative_to(root):
            raise ValueError("summary output path is outside configured output directory")
        start = window_start.astimezone(self.timezone).strftime("%Y%m%d-%H%M")
        end = window_end.astimezone(self.timezone).strftime("%Y%m%d-%H%M")
        return chat_dir / f"{start}_{end}.md"

    def _stored_output_available(
        self, output_path: Path, expected_content: str
    ) -> bool:
        if output_path.is_symlink():
            return False
        root = self.output_dir.expanduser().resolve()
        candidate = output_path.expanduser().resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return False
        try:
            return os.access(candidate, os.R_OK) and candidate.read_text(
                encoding="utf-8"
            ) == expected_content
        except (OSError, UnicodeError):
            return False

    def _remove_stale_output(self, output_path: Path) -> None:
        root = self.output_dir.expanduser().resolve()
        candidate = Path(os.path.abspath(os.path.expanduser(str(output_path))))
        canonical_parent = candidate.parent.resolve()
        if not canonical_parent.is_relative_to(root):
            raise ValueError("stale summary path is outside configured output directory")
        (canonical_parent / candidate.name).unlink(missing_ok=True)

    def _render_document(
        self,
        title: str,
        window_start: datetime,
        window_end: datetime,
        body: str,
    ) -> str:
        safe_title = re.sub(r"[\r\n]+", " ", title).strip()
        return (
            f"<!-- source: {safe_title}; window: "
            f"{self._local_time(window_start)} -> {self._local_time(window_end)} -->\n"
            f"{body}\n"
        )

    @staticmethod
    def _validate_headings(content: str) -> None:
        missing = [heading for heading in REQUIRED_HEADINGS if heading not in content]
        if missing:
            raise ValueError(f"AI summary missing required headings: {', '.join(missing)}")
