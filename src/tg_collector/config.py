from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .telegram import normalize_source_reference, source_references_may_match


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    session_path: Path
    allowlist: tuple[str, ...]
    shop_url_channels: tuple[str, ...] = ()
    api_id: int | None = None
    api_hash: str | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AIConfig:
    base_url: str | None
    model: str | None
    max_input_chars: int
    timeout_seconds: float
    max_retries: int
    prompt_version: str
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    weekday: int
    time: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    timezone: str
    database_path: Path
    summary_dir: Path
    retention_days: int
    summary_times: tuple[str, ...]
    cleanup: CleanupConfig
    telegram: TelegramConfig
    ai: AIConfig


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _validate_clock(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}", value) is None:
        raise ValueError(f"clock value must use HH:MM: {value!r}")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid clock value: {value!r}") from exc
    return parsed.strftime("%H:%M")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return value


def _reject_unknown_keys(
    value: Mapping[str, object], allowed: set[str], name: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(
            f"unknown configuration key in {name}: {', '.join(unknown)}"
        )


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    result: list[str] = []
    for item in _list(value, name):
        text = _string(item, f"{name} item").strip()
        if not text:
            raise ValueError(f"{name} must not be blank")
        result.append(text)
    return tuple(result)


def _env(environ: Mapping[str, str], key: str) -> str | None:
    value = environ.get(key)
    return value.strip() if value and value.strip() else None


def load_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = {} if loaded is None else loaded
    raw = _mapping(raw, "configuration root")
    _reject_unknown_keys(
        raw,
        {
            "timezone",
            "database_path",
            "summary_dir",
            "retention_days",
            "summary_times",
            "cleanup",
            "telegram",
            "ai",
        },
        "root",
    )
    env = os.environ if environ is None else environ
    base = config_path.parent

    timezone = _string(raw.get("timezone", "Asia/Shanghai"), "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone}") from exc

    summary_times = tuple(
        sorted(
            {
                _validate_clock(value)
                for value in _list(raw.get("summary_times", []), "summary_times")
            }
        )
    )
    if not summary_times:
        raise ValueError("summary_times must contain at least one time")

    retention_days = _integer(raw.get("retention_days", 6), "retention_days")
    if retention_days < 1:
        raise ValueError("retention_days must be positive")

    cleanup_raw = _mapping(raw.get("cleanup", {}), "cleanup")
    _reject_unknown_keys(cleanup_raw, {"weekday", "time"}, "cleanup")
    cleanup = CleanupConfig(
        weekday=_integer(cleanup_raw.get("weekday", 6), "cleanup.weekday"),
        time=_validate_clock(cleanup_raw.get("time", "03:00")),
    )
    if cleanup.weekday not in range(7):
        raise ValueError("cleanup.weekday must be between 0 and 6")

    telegram_raw = _mapping(raw.get("telegram", {}), "telegram")
    _reject_unknown_keys(
        telegram_raw,
        {"session_path", "allowlist", "shop_url_channels"},
        "telegram",
    )
    allowlist = _string_list(
        telegram_raw.get("allowlist", []), "telegram.allowlist"
    )
    if not allowlist:
        raise ValueError("telegram.allowlist must not be empty")
    api_id_text = _env(env, "TG_COLLECTOR_TELEGRAM_API_ID")
    telegram = TelegramConfig(
        session_path=_resolve_path(
            base,
            _string(
                telegram_raw.get("session_path", "data/telegram"),
                "telegram.session_path",
            ),
        ),
        allowlist=allowlist,
        shop_url_channels=_string_list(
            telegram_raw.get("shop_url_channels", []),
            "telegram.shop_url_channels",
        ),
        api_id=(
            int(api_id_text)
            if api_id_text and re.fullmatch(r"\d+", api_id_text)
            else None
        ),
        api_hash=_env(env, "TG_COLLECTOR_TELEGRAM_API_HASH"),
        phone=_env(env, "TG_COLLECTOR_TELEGRAM_PHONE"),
    )
    allowlist_references = {
        normalize_source_reference(item) for item in telegram.allowlist
    }
    outside_allowlist = [
        item
        for item in telegram.shop_url_channels
        if not any(
            source_references_may_match(normalize_source_reference(item), allowed)
            for allowed in allowlist_references
        )
    ]
    if outside_allowlist:
        raise ValueError(
            "telegram.shop_url_channels must also be present in telegram.allowlist: "
            + ", ".join(outside_allowlist)
        )

    if api_id_text and telegram.api_id is None:
        raise ValueError("TG_COLLECTOR_TELEGRAM_API_ID must be an integer")

    ai_raw = _mapping(raw.get("ai", {}), "ai")
    _reject_unknown_keys(
        ai_raw,
        {
            "base_url",
            "model",
            "max_input_chars",
            "timeout_seconds",
            "max_retries",
            "prompt_version",
        },
        "ai",
    )
    configured_base_url = ai_raw.get("base_url")
    configured_model = ai_raw.get("model")
    ai = AIConfig(
        base_url=_env(env, "TG_COLLECTOR_OPENAI_BASE_URL")
        or (
            _string(configured_base_url, "ai.base_url").strip()
            if configured_base_url is not None
            else None
        ),
        model=_env(env, "TG_COLLECTOR_OPENAI_MODEL")
        or (
            _string(configured_model, "ai.model").strip()
            if configured_model is not None
            else None
        ),
        max_input_chars=_integer(
            ai_raw.get("max_input_chars", 12000), "ai.max_input_chars"
        ),
        timeout_seconds=_number(
            ai_raw.get("timeout_seconds", 90), "ai.timeout_seconds"
        ),
        max_retries=_integer(ai_raw.get("max_retries", 3), "ai.max_retries"),
        prompt_version=_string(
            ai_raw.get("prompt_version", "v1"), "ai.prompt_version"
        ),
        api_key=_env(env, "TG_COLLECTOR_OPENAI_API_KEY"),
    )
    if ai.max_input_chars < 1000:
        raise ValueError("ai.max_input_chars must be at least 1000")
    if ai.timeout_seconds <= 0:
        raise ValueError("ai.timeout_seconds must be positive")
    if ai.max_retries < 0:
        raise ValueError("ai.max_retries must not be negative")

    return AppConfig(
        timezone=timezone,
        database_path=_resolve_path(
            base,
            _string(
                raw.get("database_path", "data/telegram.sqlite3"), "database_path"
            ),
        ),
        summary_dir=_resolve_path(
            base, _string(raw.get("summary_dir", "data/summaries"), "summary_dir")
        ),
        retention_days=retention_days,
        summary_times=summary_times,
        cleanup=cleanup,
        telegram=telegram,
        ai=ai,
    )
