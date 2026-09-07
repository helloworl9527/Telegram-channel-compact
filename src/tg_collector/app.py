from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from .schedule import closed_summary_windows, retention_cutoff
from .storage import Database

logger = logging.getLogger(__name__)


class WindowSummarizer(Protocol):
    async def summarize_window(
        self, chat_id: int, window_start: datetime, window_end: datetime
    ) -> object: ...


async def summarize_due(
    *,
    database: Database,
    service: WindowSummarizer,
    now: datetime,
    retention_days: int,
    schedule_times: tuple[str, ...],
    timezone: ZoneInfo,
) -> int:
    cutoff = retention_cutoff(now, retention_days=retention_days, tz=timezone)
    windows = [
        (max(window_start, cutoff), window_end)
        for window_start, window_end in closed_summary_windows(
            cutoff, now, schedule_times, timezone
        )
        if window_end > cutoff
    ]
    completed = 0
    for chat in database.list_chats():
        for window_start, window_end in windows:
            if not database.list_messages(chat.chat_id, window_start, window_end):
                continue
            try:
                await service.summarize_window(chat.chat_id, window_start, window_end)
            except Exception:
                logger.exception(
                    "summary failed for chat %s window %s to %s",
                    chat.chat_id,
                    window_start,
                    window_end,
                )
                continue
            else:
                completed += 1
    return completed
