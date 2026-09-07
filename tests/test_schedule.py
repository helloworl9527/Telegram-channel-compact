from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tg_collector.schedule import (
    closed_summary_windows,
    latest_weekly_cleanup,
    next_summary_window,
    retention_cutoff,
)


def test_closed_summary_windows_are_continuous_and_non_overlapping():
    tz = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 8, 28, 22, 0, tzinfo=tz)
    end = datetime(2026, 8, 29, 22, 0, tzinfo=tz)

    windows = closed_summary_windows(start, end, ("08:00", "12:00", "22:00"), tz)

    assert windows == [
        (datetime(2026, 8, 28, 22, 0, tzinfo=tz), datetime(2026, 8, 29, 8, 0, tzinfo=tz)),
        (datetime(2026, 8, 29, 8, 0, tzinfo=tz), datetime(2026, 8, 29, 12, 0, tzinfo=tz)),
        (datetime(2026, 8, 29, 12, 0, tzinfo=tz), datetime(2026, 8, 29, 22, 0, tzinfo=tz)),
    ]


def test_closed_summary_windows_exclude_future_boundary():
    tz = ZoneInfo("Asia/Shanghai")
    start = datetime(2026, 8, 29, 8, 0, tzinfo=tz)
    end = datetime(2026, 8, 29, 11, 59, tzinfo=tz)

    assert closed_summary_windows(start, end, ("08:00", "12:00", "22:00"), tz) == []


def test_latest_weekly_cleanup_is_sunday_at_three():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 31, 10, 0, tzinfo=tz)  # Monday

    cleanup = latest_weekly_cleanup(now, weekday=6, at="03:00", tz=tz)

    assert cleanup == datetime(2026, 8, 30, 3, 0, tzinfo=tz)


def test_retention_cutoff_keeps_today_and_previous_five_calendar_days():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 30, 3, 0, tzinfo=tz)

    cutoff = retention_cutoff(now, retention_days=6, tz=tz)

    assert cutoff == datetime(2026, 8, 24, 16, 0, tzinfo=UTC)


def test_next_summary_window_reports_next_boundary_and_its_window():
    tz = ZoneInfo("Asia/Shanghai")

    start, end = next_summary_window(
        datetime(2026, 8, 29, 9, tzinfo=tz),
        ("08:00", "12:00", "22:00"),
        tz,
    )

    assert start == datetime(2026, 8, 29, 8, tzinfo=tz)
    assert end == datetime(2026, 8, 29, 12, tzinfo=tz)
