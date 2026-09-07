from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo


def _parse_time(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        parsed = time(hour=int(hour_text), minute=int(minute_text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid schedule time: {value!r}") from exc
    return parsed


def _local_boundary(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz)


def closed_summary_windows(
    start: datetime,
    end: datetime,
    schedule_times: tuple[str, ...],
    tz: ZoneInfo,
) -> list[tuple[datetime, datetime]]:
    """Return complete scheduled windows that closed in ``(start, end]``."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end < start:
        raise ValueError("end must not be earlier than start")

    parsed_times = sorted({_parse_time(value) for value in schedule_times})
    if not parsed_times:
        raise ValueError("at least one summary time is required")

    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    first_day = local_start.date() - timedelta(days=1)
    last_day = local_end.date() + timedelta(days=1)

    boundaries: list[datetime] = []
    day = first_day
    while day <= last_day:
        boundaries.extend(_local_boundary(day, at, tz) for at in parsed_times)
        day += timedelta(days=1)
    boundaries.sort()

    windows: list[tuple[datetime, datetime]] = []
    for window_start, window_end in pairwise(boundaries):
        if window_end > local_start and window_end <= local_end:
            windows.append((window_start, window_end))
    return windows


def next_summary_window(
    now: datetime,
    schedule_times: tuple[str, ...],
    tz: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return the next scheduled execution boundary and its preceding window."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    parsed_times = sorted({_parse_time(value) for value in schedule_times})
    if not parsed_times:
        raise ValueError("at least one summary time is required")
    local_now = now.astimezone(tz)
    boundaries: list[datetime] = []
    for offset in range(-1, 3):
        day = local_now.date() + timedelta(days=offset)
        boundaries.extend(_local_boundary(day, at, tz) for at in parsed_times)
    boundaries.sort()
    next_boundary = next(boundary for boundary in boundaries if boundary > local_now)
    previous_boundary = max(boundary for boundary in boundaries if boundary < next_boundary)
    return previous_boundary, next_boundary


def latest_weekly_cleanup(
    now: datetime,
    *,
    weekday: int,
    at: str,
    tz: ZoneInfo,
) -> datetime:
    """Return the latest weekly cleanup boundary at or before ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if weekday not in range(7):
        raise ValueError("weekday must be between 0 and 6")

    local_now = now.astimezone(tz)
    cleanup_time = _parse_time(at)
    days_since = (local_now.weekday() - weekday) % 7
    candidate_day = local_now.date() - timedelta(days=days_since)
    candidate = _local_boundary(candidate_day, cleanup_time, tz)
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate


def retention_cutoff(now: datetime, *, retention_days: int, tz: ZoneInfo) -> datetime:
    """Return the UTC start of the oldest retained local calendar day."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    local_now = now.astimezone(tz)
    cutoff_day = local_now.date() - timedelta(days=retention_days - 1)
    return datetime.combine(cutoff_day, time.min, tzinfo=tz).astimezone(UTC)
