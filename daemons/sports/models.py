"""Persistence models for sports reservation jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sjtusuite.clients.sports import (
    SLOT_SELECTION_MODE_ALL_REQUIRED,
    PreparedTargetDateReservation,
    SportsReservationClient,
)

from .constants import JOB_TYPE_TARGET_DATE, TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS
from .utils import (
    _default_retry_interval_seconds,
    _next_noon_run_date_iso,
    _normalize_fields,
    _normalize_time_slots,
    _now_iso,
    _parse_datetime,
    _run_date_message,
)


@dataclass(slots=True)
class ReservationJob:
    job_id: str
    name: str
    venue_id: str
    venue_name: str
    motion: str
    job_type: str
    target_date: str
    time_slots: list[str]
    run_on_date: str | None = None
    slot_selection_mode: str = SLOT_SELECTION_MODE_ALL_REQUIRED
    preferred_fields: list[str] = field(default_factory=list)
    enabled: bool = True
    retry_window_seconds: int = 180
    retry_interval_seconds: float = TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS
    auto_disable_on_success: bool = True
    cron_interval_minutes: int = 10
    window_start_days: int = 0
    window_end_days: int = 7
    redeem_deadline_hours: int = 2
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_checked_at: str | None = None
    last_status: str = "idle"
    last_message: str = "Waiting for the noon reservation window."
    last_order_id: str | None = None
    last_payment_url: str | None = None
    last_payment_init: dict[str, Any] | None = None
    success_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReservationJob":
        created_at = payload.get("created_at", _now_iso())
        run_on_date = payload.get("run_on_date")
        if run_on_date is not None:
            run_on_date = (run_on_date or "").strip() or None
        legacy_created_at = None
        if (
            run_on_date is None
            and payload.get("job_type", JOB_TYPE_TARGET_DATE) == JOB_TYPE_TARGET_DATE
        ):
            try:
                legacy_created_at = _parse_datetime(created_at)
            except ValueError:
                legacy_created_at = None
        return cls(
            job_id=payload["job_id"],
            name=payload["name"],
            venue_id=payload["venue_id"],
            venue_name=payload.get("venue_name", payload["venue_id"]),
            motion=payload["motion"],
            job_type=payload.get("job_type", JOB_TYPE_TARGET_DATE),
            target_date=payload.get("target_date", ""),
            run_on_date=run_on_date if run_on_date is not None else None,
            slot_selection_mode=payload.get(
                "slot_selection_mode", SLOT_SELECTION_MODE_ALL_REQUIRED
            ),
            time_slots=_normalize_time_slots(payload.get("time_slots", [])),
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=int(payload.get("retry_window_seconds", 180)),
            retry_interval_seconds=float(
                payload.get(
                    "retry_interval_seconds",
                    _default_retry_interval_seconds(
                        payload.get("job_type", JOB_TYPE_TARGET_DATE)
                    ),
                )
            ),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
            cron_interval_minutes=max(1, int(payload.get("cron_interval_minutes", 10))),
            window_start_days=max(0, int(payload.get("window_start_days", 0))),
            window_end_days=max(0, int(payload.get("window_end_days", 7))),
            redeem_deadline_hours=max(0, int(payload.get("redeem_deadline_hours", 2))),
            created_at=created_at,
            updated_at=payload.get("updated_at", _now_iso()),
            last_checked_at=payload.get("last_checked_at"),
            last_status=payload.get("last_status", "idle"),
            last_message=payload.get(
                "last_message",
                _run_date_message(_next_noon_run_date_iso(legacy_created_at))
                if legacy_created_at
                else "",
            ),
            last_order_id=payload.get("last_order_id"),
            last_payment_url=payload.get("last_payment_url"),
            last_payment_init=payload.get("last_payment_init"),
            success_at=payload.get("success_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PreparedRunContext:
    client: SportsReservationClient
    prepared_job: PreparedTargetDateReservation
    warmed_at_monotonic: float
