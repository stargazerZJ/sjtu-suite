"""Utility helpers for sports reservation scheduling and persistence."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sjtusuite.clients.sports import (
    SPORTS_TIME_SLOTS,
    PreparedTargetDateReservation,
)

from .constants import (
    JOB_TYPE_TARGET_DATE,
    LOG_FILE,
    TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS,
)


def _default_retry_interval_seconds(job_type: str) -> float:
    return (
        TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS
        if job_type == JOB_TYPE_TARGET_DATE
        else 1.0
    )


def _is_transport_error(message: str) -> bool:
    prefixes = (
        "Sports request failed",
        "Sports login request failed",
        "Sports login failed",
    )
    return any(message.startswith(prefix) for prefix in prefixes)


def _clone_prepared_target_date_reservation(
    prepared: PreparedTargetDateReservation | None,
) -> PreparedTargetDateReservation | None:
    if prepared is None:
        return None
    return PreparedTargetDateReservation(
        motion_type=prepared.motion_type,
        date_option=prepared.date_option,
    )


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("sports_server")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOGGER = _configure_logger()


def _format_log_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _format_log_message(message: str, **fields: Any) -> str:
    if not fields:
        return message
    serialized = ", ".join(
        f"{key}={_format_log_value(value)}" for key, value in fields.items()
    )
    return f"{message} | {serialized}"


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_date(value: str) -> datetime.date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _next_noon_run_date(reference: datetime | None = None) -> datetime.date:
    now = reference or _now()
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    return now.date() if now <= noon else (now + timedelta(days=1)).date()


def _next_noon_run_date_iso(reference: datetime | None = None) -> str:
    return _next_noon_run_date(reference).isoformat()


def _run_date_message(run_on_date: str | None) -> str:
    if run_on_date:
        return f"Waiting for scheduled noon run on {run_on_date}."
    return "Waiting for the noon reservation window."


def _cron_waiting_message() -> str:
    return "Waiting for the next cron scan."


def _normalize_time_slots(values: list[str] | tuple[str, ...] | str) -> list[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    seen: set[str] = set()
    normalized: list[str] = []
    for slot in values:
        if slot not in SPORTS_TIME_SLOTS or slot in seen:
            continue
        seen.add(slot)
        normalized.append(slot)
    return normalized


def _normalize_fields(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    return [item for item in values if item]
