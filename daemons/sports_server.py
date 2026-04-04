"""Sports reservation daemon with a web dashboard and noon scheduler."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, render_template_string, request
from sjtusuite.auth import JACLogin
from sjtusuite.clients.sports import (
    SLOT_SELECTION_MODE_ALL_REQUIRED,
    SLOT_SELECTION_MODE_FIRST_AVAILABLE,
    SPORTS_TIME_SLOTS,
    PreparedTargetDateReservation,
    SportsAPIError,
    SportsReservationClient,
)
from sjtusuite.core.config import get_data_dir
from sjtusuite.core.credentials import credentials
from sjtusuite.notifications import NtfyNotifier
from sjtusuite.servers.base import get_client_ip

TIMEZONE = "Asia/Shanghai"
JOBS_FILE = get_data_dir() / "sports_reservation_jobs.json"
LOG_FILE = get_data_dir() / "sports_reservation_daemon.log"
JOB_TYPE_TARGET_DATE = "target_date"
JOB_TYPE_CRON = "cron"
NOON_WARMUP_HOUR = 11
NOON_WARMUP_MINUTE = 59
NOON_WARMUP_SECOND = 00
PREPARED_CONTEXT_TTL_SECONDS = 240
TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS = 0.2
TARGET_DATE_BURST_WINDOW_SECONDS = 15.0
TARGET_DATE_PREVIEW_BURST_CONCURRENCY = 4


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


class SportsReservationDaemon:
    def __init__(
        self,
        *,
        use_browser_session: bool = False,
        jobs_file: Path = JOBS_FILE,
    ):
        self.logger = LOGGER
        self.use_browser_session = use_browser_session
        self.jobs_file = jobs_file
        self.jobs_lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.history = deque(maxlen=100)
        self.prepared_contexts_lock = threading.Lock()
        self.prepared_contexts: dict[str, PreparedRunContext] = {}
        self.scheduler = BackgroundScheduler(timezone=TIMEZONE)
        self.scheduler.add_job(
            self.run_warmup_jobs,
            trigger=CronTrigger(
                hour=NOON_WARMUP_HOUR,
                minute=NOON_WARMUP_MINUTE,
                second=NOON_WARMUP_SECOND,
                timezone=TIMEZONE,
            ),
            id="sports-noon-warmup",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self.run_scheduled_jobs,
            trigger=CronTrigger(hour=12, minute=0, timezone=TIMEZONE),
            id="sports-noon-run",
            replace_existing=True,
        )
        self.jobs: dict[str, ReservationJob] = self._load_jobs()
        self.notifier = NtfyNotifier.from_config(credentials.ntfy_config)
        self._sync_cron_jobs()
        self._log(
            logging.INFO,
            "Sports reservation daemon initialized.",
            auth_mode="browser" if self.use_browser_session else "credentials",
            jobs_file=self.jobs_file,
            job_count=len(self.jobs),
            log_file=LOG_FILE,
        )

    def _log(self, level: int, message: str, **fields: Any) -> None:
        self.logger.log(level, _format_log_message(message, **fields))

    @staticmethod
    def _job_fields(job: ReservationJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "job_name": job.name,
            "job_type": job.job_type,
            "venue_id": job.venue_id,
            "venue_name": job.venue_name,
            "motion": job.motion,
            "target_date": job.target_date,
            "run_on_date": job.run_on_date,
            "slot_selection_mode": job.slot_selection_mode,
            "time_slots": job.time_slots,
            "preferred_fields": job.preferred_fields,
        }

    @staticmethod
    def _preview_for_log(preview: dict[str, Any]) -> dict[str, Any]:
        return {
            "date_option": preview.get("date_option"),
            "selected_slots": preview.get("selected_slots"),
            "total_price": preview.get("total_price"),
        }

    def _discard_stale_prepared_contexts(self) -> None:
        now = time.monotonic()
        stale_job_ids: list[str] = []
        with self.prepared_contexts_lock:
            for job_id, context in self.prepared_contexts.items():
                if now - context.warmed_at_monotonic > PREPARED_CONTEXT_TTL_SECONDS:
                    stale_job_ids.append(job_id)
            for job_id in stale_job_ids:
                self.prepared_contexts.pop(job_id, None)
        for job_id in stale_job_ids:
            self._log(
                logging.DEBUG,
                "Discarded stale prepared reservation context.",
                job_id=job_id,
            )

    def _store_prepared_context(
        self,
        job: ReservationJob,
        *,
        client: SportsReservationClient,
        prepared_job: PreparedTargetDateReservation,
    ) -> None:
        with self.prepared_contexts_lock:
            self.prepared_contexts[job.job_id] = PreparedRunContext(
                client=client,
                prepared_job=prepared_job,
                warmed_at_monotonic=time.monotonic(),
            )
        self._log(
            logging.DEBUG,
            "Stored prepared reservation context.",
            **self._job_fields(job),
        )

    def _take_prepared_context(self, job: ReservationJob) -> PreparedRunContext | None:
        self._discard_stale_prepared_contexts()
        with self.prepared_contexts_lock:
            context = self.prepared_contexts.pop(job.job_id, None)
        if context:
            self._log(
                logging.DEBUG,
                "Reusing prepared reservation context.",
                **self._job_fields(job),
            )
        return context

    def start(self) -> None:
        self._log(logging.INFO, "Starting sports reservation scheduler.")
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self._log(logging.INFO, "Shutting down sports reservation scheduler.")
            self.scheduler.shutdown(wait=False)

    def create_client(self) -> SportsReservationClient:
        self._log(
            logging.DEBUG,
            "Creating sports reservation client.",
            auth_mode="browser" if self.use_browser_session else "credentials",
        )
        jac_login = JACLogin(credentials.username or "", credentials.password or "")
        client = SportsReservationClient(jac_login)
        if self.use_browser_session:
            client.load_playwright_browser_session()
            self._log(
                logging.DEBUG, "Sports reservation client ready using browser session."
            )
            return client
        if not credentials.username or not credentials.password:
            raise SportsAPIError(
                "Daemon mode needs credentials.json or env credentials unless it is started with --from-browser."
            )
        client.login()
        self._log(
            logging.DEBUG, "Sports reservation client login completed with credentials."
        )
        return client

    def _load_jobs(self) -> dict[str, ReservationJob]:
        if not self.jobs_file.exists():
            self._log(
                logging.DEBUG, "Jobs file does not exist yet.", jobs_file=self.jobs_file
            )
            return {}
        with self.jobs_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        jobs = payload.get("jobs", [])
        self._log(
            logging.DEBUG,
            "Loaded sports reservation jobs from disk.",
            jobs_file=self.jobs_file,
            job_count=len(jobs),
        )
        return {item["job_id"]: ReservationJob.from_dict(item) for item in jobs}

    def _save_jobs(self) -> None:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now_iso(),
            "jobs": [job.to_dict() for job in self.list_jobs()],
        }
        with self.jobs_file.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self._log(
            logging.DEBUG,
            "Persisted sports reservation jobs to disk.",
            jobs_file=self.jobs_file,
            job_count=len(payload["jobs"]),
        )

    def list_jobs(self) -> list[ReservationJob]:
        with self.jobs_lock:
            return sorted(
                self.jobs.values(),
                key=lambda job: (
                    job.target_date or "9999-12-31",
                    job.name,
                    job.created_at,
                ),
            )

    @staticmethod
    def _validate_job_type(job_type: str) -> str:
        normalized = (job_type or JOB_TYPE_TARGET_DATE).strip()
        if normalized not in {JOB_TYPE_TARGET_DATE, JOB_TYPE_CRON}:
            raise ValueError(f"Unknown job_type: {job_type}")
        return normalized

    @staticmethod
    def _validate_slot_selection_mode(slot_selection_mode: str) -> str:
        normalized = (slot_selection_mode or SLOT_SELECTION_MODE_ALL_REQUIRED).strip()
        if normalized not in {
            SLOT_SELECTION_MODE_ALL_REQUIRED,
            SLOT_SELECTION_MODE_FIRST_AVAILABLE,
        }:
            raise ValueError(f"Unknown slot_selection_mode: {slot_selection_mode}")
        return normalized

    @staticmethod
    def _cron_scheduler_job_id(job_id: str) -> str:
        return f"sports-cron-{job_id}"

    def _sync_cron_jobs(self) -> None:
        desired: dict[str, ReservationJob] = {}
        for job in self.jobs.values():
            if job.job_type == JOB_TYPE_CRON and job.enabled:
                desired[self._cron_scheduler_job_id(job.job_id)] = job

        current_ids = {
            item.id
            for item in self.scheduler.get_jobs()
            if item.id.startswith("sports-cron-")
        }
        for stale_id in current_ids - set(desired):
            try:
                self.scheduler.remove_job(stale_id)
            except JobLookupError:
                pass

        for scheduler_id, job in desired.items():
            self.scheduler.add_job(
                self.run_job,
                trigger=IntervalTrigger(
                    minutes=job.cron_interval_minutes, timezone=TIMEZONE
                ),
                id=scheduler_id,
                replace_existing=True,
                kwargs={
                    "job_id": job.job_id,
                    "dry_run": False,
                    "triggered_by": JOB_TYPE_CRON,
                },
                coalesce=True,
                max_instances=1,
            )

    @staticmethod
    def _normalize_run_on_date(value: Any) -> str | None:
        text = (value or "").strip()
        if not text:
            return None
        _parse_date(text)
        return text

    @staticmethod
    def _validate_target_run_on_date(run_on_date: str) -> str:
        scheduled_date = _parse_date(run_on_date)
        scheduled_noon = datetime.combine(scheduled_date, datetime.min.time()).replace(
            hour=12
        )
        if scheduled_noon < _now():
            raise ValueError("Choose a scheduled noon that has not passed yet.")
        return run_on_date

    def get_job(self, job_id: str) -> ReservationJob:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job

    def create_job(self, payload: dict[str, Any]) -> ReservationJob:
        self._log(
            logging.DEBUG, "Creating reservation job from payload.", payload=payload
        )
        time_slots = _normalize_time_slots(payload.get("time_slots", []))
        if not time_slots:
            raise ValueError("Choose at least one time slot.")

        job_type = self._validate_job_type(
            payload.get("job_type", JOB_TYPE_TARGET_DATE)
        )
        slot_selection_mode = self._validate_slot_selection_mode(
            payload.get("slot_selection_mode", SLOT_SELECTION_MODE_ALL_REQUIRED)
        )
        target_date = (payload.get("target_date") or "").strip()
        run_on_date = self._normalize_run_on_date(payload.get("run_on_date"))
        if job_type == JOB_TYPE_TARGET_DATE:
            if not target_date:
                raise ValueError("Choose a target date.")
            _parse_date(target_date)
            run_on_date = self._validate_target_run_on_date(
                run_on_date or _next_noon_run_date_iso()
            )
        else:
            run_on_date = None

        window_start_days = max(0, int(payload.get("window_start_days", 0)))
        window_end_days = max(0, int(payload.get("window_end_days", 7)))
        if window_end_days < window_start_days:
            raise ValueError(
                "window_end_days must be greater than or equal to window_start_days."
            )

        default_retry_interval = _default_retry_interval_seconds(job_type)
        job = ReservationJob(
            job_id=str(uuid.uuid4()),
            name=(payload.get("name") or "").strip()
            or (
                f"{payload['motion']} {target_date}"
                if job_type == JOB_TYPE_TARGET_DATE
                else f"{payload['motion']} cron"
            ),
            venue_id=payload["venue_id"].strip(),
            venue_name=(payload.get("venue_name") or payload["venue_id"]).strip(),
            motion=payload["motion"].strip(),
            job_type=job_type,
            target_date=target_date,
            run_on_date=run_on_date,
            slot_selection_mode=slot_selection_mode,
            time_slots=time_slots,
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=max(10, int(payload.get("retry_window_seconds", 180))),
            retry_interval_seconds=max(
                0.2,
                float(payload.get("retry_interval_seconds", default_retry_interval)),
            ),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
            cron_interval_minutes=max(1, int(payload.get("cron_interval_minutes", 10))),
            window_start_days=window_start_days,
            window_end_days=window_end_days,
            redeem_deadline_hours=max(0, int(payload.get("redeem_deadline_hours", 2))),
            last_status="waiting_schedule"
            if job_type == JOB_TYPE_TARGET_DATE
            else "idle",
            last_message=_run_date_message(run_on_date)
            if job_type == JOB_TYPE_TARGET_DATE
            else _cron_waiting_message(),
        )
        with self.jobs_lock:
            self.jobs[job.job_id] = job
            self._save_jobs()
            self._sync_cron_jobs()
        created_message = "Reservation job created."
        if job.job_type == JOB_TYPE_TARGET_DATE and job.run_on_date:
            created_message = f"Reservation job created for noon on {job.run_on_date}."
        self._log(logging.INFO, "Reservation job created.", **self._job_fields(job))
        self.record_history(job, "created", True, created_message)
        return job

    def update_job(self, job_id: str, payload: dict[str, Any]) -> ReservationJob:
        self._log(
            logging.DEBUG, "Updating reservation job.", job_id=job_id, payload=payload
        )
        with self.jobs_lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            if "name" in payload:
                job.name = payload["name"].strip() or job.name
            if "venue_id" in payload:
                job.venue_id = payload["venue_id"].strip()
            if "venue_name" in payload:
                job.venue_name = payload["venue_name"].strip() or job.venue_name
            if "motion" in payload:
                job.motion = payload["motion"].strip()
            if "job_type" in payload:
                job.job_type = self._validate_job_type(payload["job_type"])
            if "slot_selection_mode" in payload:
                job.slot_selection_mode = self._validate_slot_selection_mode(
                    payload["slot_selection_mode"]
                )
            if "target_date" in payload:
                target_date = (payload["target_date"] or "").strip()
                if job.job_type == JOB_TYPE_TARGET_DATE:
                    if not target_date:
                        raise ValueError("Choose a target date.")
                    _parse_date(target_date)
                job.target_date = target_date
            if "run_on_date" in payload:
                job.run_on_date = self._normalize_run_on_date(payload["run_on_date"])
            elif (
                "job_type" in payload
                and job.job_type == JOB_TYPE_TARGET_DATE
                and not job.run_on_date
            ):
                job.run_on_date = _next_noon_run_date_iso()
            if "time_slots" in payload:
                time_slots = _normalize_time_slots(payload["time_slots"])
                if not time_slots:
                    raise ValueError("Choose at least one time slot.")
                job.time_slots = time_slots
            if "preferred_fields" in payload:
                job.preferred_fields = _normalize_fields(payload["preferred_fields"])
            if "enabled" in payload:
                job.enabled = bool(payload["enabled"])
            if "retry_window_seconds" in payload:
                job.retry_window_seconds = max(10, int(payload["retry_window_seconds"]))
            if "retry_interval_seconds" in payload:
                job.retry_interval_seconds = max(
                    0.2, float(payload["retry_interval_seconds"])
                )
            if "auto_disable_on_success" in payload:
                job.auto_disable_on_success = bool(payload["auto_disable_on_success"])
            if "cron_interval_minutes" in payload:
                job.cron_interval_minutes = max(
                    1, int(payload["cron_interval_minutes"])
                )
            if "window_start_days" in payload:
                job.window_start_days = max(0, int(payload["window_start_days"]))
            if "window_end_days" in payload:
                job.window_end_days = max(0, int(payload["window_end_days"]))
            if "redeem_deadline_hours" in payload:
                job.redeem_deadline_hours = max(
                    0, int(payload["redeem_deadline_hours"])
                )

            if job.job_type == JOB_TYPE_TARGET_DATE and not job.target_date:
                raise ValueError("Choose a target date.")
            if job.job_type == JOB_TYPE_TARGET_DATE:
                job.run_on_date = self._validate_target_run_on_date(
                    job.run_on_date or _next_noon_run_date_iso()
                )
                if job.last_status in {"idle", "waiting_schedule"}:
                    job.last_message = _run_date_message(job.run_on_date)
                    job.last_status = "waiting_schedule"
            else:
                job.run_on_date = None
                if job.last_status in {"idle", "waiting_schedule"}:
                    job.last_message = _cron_waiting_message()
                    job.last_status = "idle"
            if job.window_end_days < job.window_start_days:
                raise ValueError(
                    "window_end_days must be greater than or equal to window_start_days."
                )
            job.updated_at = _now_iso()
            self._save_jobs()
            self._sync_cron_jobs()
            self._log(logging.INFO, "Reservation job updated.", **self._job_fields(job))
            return job

    def delete_job(self, job_id: str) -> None:
        with self.jobs_lock:
            job = self.jobs.pop(job_id, None)
            if not job:
                raise KeyError(job_id)
            self._save_jobs()
            self._sync_cron_jobs()
        self._log(logging.INFO, "Reservation job deleted.", **self._job_fields(job))
        self.record_history(job, "deleted", True, "Reservation job deleted.")

    def record_history(
        self,
        job: ReservationJob,
        action: str,
        success: bool,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._log(
            logging.DEBUG,
            "Recording reservation history event.",
            **self._job_fields(job),
            action=action,
            success=success,
            history_message=message,
            details=details or {},
        )
        self.history.appendleft(
            {
                "timestamp": _now_iso(),
                "job_id": job.job_id,
                "job_name": job.name,
                "action": action,
                "success": success,
                "message": message,
                "details": details or {},
            }
        )

    def service_status(self) -> dict[str, Any]:
        next_run = None
        scheduled_job = self.scheduler.get_job("sports-noon-run")
        next_run_time = (
            getattr(scheduled_job, "next_run_time", None) if scheduled_job else None
        )
        if next_run_time:
            next_run = next_run_time.isoformat()
        return {
            "auth_mode": "browser" if self.use_browser_session else "credentials",
            "next_run_at": next_run,
            "jobs": [job.to_dict() for job in self.list_jobs()],
            "history": list(self.history)[:30],
        }

    def list_venues(self, search: str = "") -> list[dict[str, Any]]:
        self._log(logging.DEBUG, "Listing venues for dashboard search.", search=search)
        client = self.create_client()
        venues, _ = client.list_all_venues(venue_name=search)
        self._log(
            logging.DEBUG,
            "Venue search completed.",
            search=search,
            venue_count=len(venues),
        )
        return [venue.raw for venue in venues]

    def get_venue_detail(self, venue_id: str) -> dict[str, Any]:
        self._log(logging.DEBUG, "Fetching venue detail.", venue_id=venue_id)
        client = self.create_client()
        detail = client.get_venue_detail(venue_id)
        self._log(
            logging.DEBUG,
            "Venue detail fetched.",
            venue_id=venue_id,
            motion_type_count=len(detail.motion_types),
        )
        return {
            "venue_id": detail.venue_id,
            "venue_name": detail.venue_name,
            "campus_name": detail.campus_name,
            "open_time": detail.open_time,
            "venue_mobile": detail.venue_mobile,
            "motion_types": [asdict(item) for item in detail.motion_types],
        }

    def get_availability(self, venue_id: str, motion: str) -> list[dict[str, Any]]:
        self._log(
            logging.DEBUG,
            "Fetching venue availability.",
            venue_id=venue_id,
            motion=motion,
        )
        client = self.create_client()
        results = client.list_availability(venue_id, motion)
        self._log(
            logging.DEBUG,
            "Venue availability fetched.",
            venue_id=venue_id,
            motion=motion,
            visible_dates=len(results),
        )
        return results

    def build_job_preview(
        self,
        client: SportsReservationClient,
        job: ReservationJob,
        *,
        prepared: PreparedTargetDateReservation | None = None,
    ) -> dict[str, Any]:
        if job.job_type == JOB_TYPE_CRON:
            self._log(
                logging.DEBUG, "Building cron booking preview.", **self._job_fields(job)
            )
            preview = client.build_cron_preview(
                venue_id=job.venue_id,
                motion=job.motion,
                time_slots=job.time_slots,
                preferred_fields=job.preferred_fields,
                slot_selection_mode=job.slot_selection_mode,
                window_start_days=job.window_start_days,
                window_end_days=job.window_end_days,
                redeem_deadline_hours=job.redeem_deadline_hours,
                now=_now(),
            )
        else:
            self._log(
                logging.DEBUG,
                "Building target-date booking preview.",
                **self._job_fields(job),
            )
            preview = client.build_target_date_preview(
                venue_id=job.venue_id,
                motion=job.motion,
                target_date=job.target_date,
                time_slots=job.time_slots,
                preferred_fields=job.preferred_fields,
                slot_selection_mode=job.slot_selection_mode,
                prepared=prepared,
            )
        preview_dict = preview.to_dict()
        self._log(
            logging.DEBUG,
            "Built booking preview.",
            **self._job_fields(job),
            preview=self._preview_for_log(preview_dict),
        )
        return preview_dict

    def _mark_job(
        self,
        job: ReservationJob,
        *,
        status: str,
        message: str,
        checked_at: str | None = None,
        order_id: str | None = None,
        payment: dict[str, Any] | None = None,
        success: bool = False,
        persist: bool = True,
    ) -> None:
        with self.jobs_lock:
            job.last_status = status
            job.last_message = message
            job.last_checked_at = checked_at or _now_iso()
            job.updated_at = _now_iso()
            if order_id:
                job.last_order_id = order_id
            if payment:
                job.last_payment_init = payment
                job.last_payment_url = payment.get("payment_url")
            if success:
                job.success_at = _now_iso()
                if job.auto_disable_on_success:
                    job.enabled = False
            if persist:
                self._save_jobs()
                self._sync_cron_jobs()
        self._log(
            logging.DEBUG,
            "Updated reservation job state.",
            **self._job_fields(job),
            status=status,
            state_message=message,
            checked_at=job.last_checked_at,
            order_id=order_id,
            payment_url=job.last_payment_url,
            success=success,
            enabled=job.enabled,
            persisted=persist,
        )

    def _effective_retry_interval_seconds(
        self, job: ReservationJob, *, triggered_by: str
    ) -> float:
        if job.job_type == JOB_TYPE_TARGET_DATE and triggered_by == "schedule":
            return min(
                job.retry_interval_seconds, TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS
            )
        return job.retry_interval_seconds

    def _should_use_preview_burst(
        self,
        job: ReservationJob,
        *,
        triggered_by: str,
    ) -> bool:
        if (
            job.job_type != JOB_TYPE_TARGET_DATE
            or triggered_by != "schedule"
            or not job.run_on_date
        ):
            return False
        scheduled_noon = datetime.combine(
            _parse_date(job.run_on_date), datetime.min.time()
        ).replace(hour=12)
        elapsed = (_now() - scheduled_noon).total_seconds()
        return 0 <= elapsed <= TARGET_DATE_BURST_WINDOW_SECONDS

    def _build_job_preview_with_burst(
        self,
        client: SportsReservationClient,
        job: ReservationJob,
        *,
        prepared: PreparedTargetDateReservation | None,
        triggered_by: str,
    ) -> dict[str, Any]:
        if not self._should_use_preview_burst(job, triggered_by=triggered_by):
            return self.build_job_preview(client, job, prepared=prepared)

        worker_count = TARGET_DATE_PREVIEW_BURST_CONCURRENCY
        self._log(
            logging.DEBUG,
            "Running concurrent preview burst.",
            **self._job_fields(job),
            worker_count=worker_count,
        )

        def worker(worker_index: int) -> dict[str, Any]:
            worker_client = client.clone_with_session(
                name_suffix=f"burst-{worker_index}"
            )
            worker_prepared = _clone_prepared_target_date_reservation(prepared)
            return self.build_job_preview(worker_client, job, prepared=worker_prepared)

        failures: list[SportsAPIError] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=worker_count
        ) as executor:
            futures = [
                executor.submit(worker, index) for index in range(1, worker_count + 1)
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    try:
                        preview = future.result()
                        self._log(
                            logging.DEBUG,
                            "Concurrent preview burst succeeded.",
                            **self._job_fields(job),
                        )
                        for pending in futures:
                            if pending is not future:
                                pending.cancel()
                        return preview
                    except SportsAPIError as exc:
                        failures.append(exc)
            finally:
                for future in futures:
                    future.cancel()

        if failures:
            raise failures[0]
        raise SportsAPIError("Concurrent preview burst failed without a result.")

    def run_warmup_jobs(self) -> None:
        today = _now().date()
        self._discard_stale_prepared_contexts()
        self._log(
            logging.INFO,
            "Running pre-noon warmup for target-date reservation jobs.",
            today=today.isoformat(),
        )
        for job in self.list_jobs():
            if not job.enabled or job.job_type != JOB_TYPE_TARGET_DATE:
                continue
            if not job.run_on_date:
                continue
            if _parse_date(job.run_on_date) != today:
                continue
            try:
                self._log(
                    logging.INFO,
                    "Preparing target-date reservation job before noon.",
                    **self._job_fields(job),
                )
                client = self.create_client()
                prepared_job = client.prepare_target_date_reservation(
                    venue_id=job.venue_id,
                    motion=job.motion,
                )
                self._store_prepared_context(
                    job, client=client, prepared_job=prepared_job
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.exception(
                    _format_log_message(
                        "Failed to warm up target-date reservation job.",
                        **self._job_fields(job),
                        error=str(exc),
                    )
                )

    def run_job(
        self,
        job_id: str,
        *,
        dry_run: bool = False,
        triggered_by: str = "manual",
    ) -> dict[str, Any]:
        with self.run_lock:
            job = self.get_job(job_id)
            self._log(
                logging.INFO,
                "Starting reservation job run.",
                **self._job_fields(job),
                dry_run=dry_run,
                triggered_by=triggered_by,
            )
            today = _now().date()
            if job.job_type == JOB_TYPE_TARGET_DATE:
                target_date = _parse_date(job.target_date)
                if target_date < today:
                    self._mark_job(
                        job, status="expired", message="Target date has already passed."
                    )
                    self.record_history(
                        job, triggered_by, False, "Target date has already passed."
                    )
                    return {"ok": False, "message": "Target date has already passed."}
            if (
                job.job_type == JOB_TYPE_TARGET_DATE
                and triggered_by == "schedule"
                and _parse_date(job.target_date) > today + timedelta(days=7)
            ):
                message = "Still outside the current booking window."
                self._mark_job(job, status="waiting_window", message=message)
                self.record_history(job, triggered_by, True, message)
                return {"ok": True, "message": message}
            if (
                not dry_run
                and not job.enabled
                and triggered_by in {"schedule", JOB_TYPE_CRON}
            ):
                return {"ok": False, "message": "Job is disabled."}

            prepared_context = (
                self._take_prepared_context(job)
                if job.job_type == JOB_TYPE_TARGET_DATE and triggered_by == "schedule"
                else None
            )
            client = prepared_context.client if prepared_context else None
            prepared_target_job = (
                prepared_context.prepared_job if prepared_context else None
            )
            retry_window_seconds = (
                job.retry_window_seconds if job.job_type == JOB_TYPE_TARGET_DATE else 0
            )
            deadline = time.time() + (retry_window_seconds if not dry_run else 0)
            attempt_count = 0
            while True:
                attempt_count += 1
                remaining_retry_window = (
                    max(0.0, deadline - time.time()) if not dry_run else 0.0
                )
                retry_sleep_seconds = self._effective_retry_interval_seconds(
                    job, triggered_by=triggered_by
                )
                self._log(
                    logging.DEBUG,
                    "Starting reservation attempt.",
                    **self._job_fields(job),
                    attempt=attempt_count,
                    dry_run=dry_run,
                    triggered_by=triggered_by,
                    retry_window_seconds=retry_window_seconds,
                    retry_interval_seconds=retry_sleep_seconds,
                    retry_window_remaining=f"{remaining_retry_window:.3f}",
                )
                try:
                    if client is None:
                        client = self.create_client()
                    if (
                        job.job_type == JOB_TYPE_TARGET_DATE
                        and prepared_target_job is None
                    ):
                        prepared_target_job = client.prepare_target_date_reservation(
                            venue_id=job.venue_id,
                            motion=job.motion,
                        )
                except SportsAPIError as exc:
                    message = str(exc)
                    should_retry = (
                        not dry_run
                        and triggered_by in {"schedule", "manual"}
                        and time.time() + retry_sleep_seconds <= deadline
                    )
                    self._log(
                        logging.WARNING,
                        "Client setup failed for reservation attempt.",
                        **self._job_fields(job),
                        attempt=attempt_count,
                        error=message,
                        should_retry=should_retry,
                        retry_sleep_seconds=retry_sleep_seconds if should_retry else 0,
                    )
                    client = None
                    self._mark_job(
                        job,
                        status="submit_failed",
                        message=message,
                        persist=not should_retry,
                    )
                    if should_retry:
                        self._log(
                            logging.DEBUG,
                            "Sleeping before retry after client setup failure.",
                            **self._job_fields(job),
                            attempt=attempt_count,
                            sleep_seconds=retry_sleep_seconds,
                        )
                        time.sleep(retry_sleep_seconds)
                        continue
                    self.record_history(
                        job,
                        triggered_by,
                        False,
                        message,
                        details={"attempts": attempt_count},
                    )
                    return {"ok": False, "message": message, "attempts": attempt_count}
                try:
                    preview = self._build_job_preview_with_burst(
                        client,
                        job,
                        prepared=prepared_target_job,
                        triggered_by=triggered_by,
                    )
                except SportsAPIError as exc:
                    message = str(exc)
                    should_retry = (
                        not dry_run
                        and triggered_by in {"schedule", "manual"}
                        and time.time() + retry_sleep_seconds <= deadline
                    )
                    self._log(
                        logging.WARNING,
                        "Preview build failed for reservation attempt.",
                        **self._job_fields(job),
                        attempt=attempt_count,
                        error=message,
                        should_retry=should_retry,
                        retry_sleep_seconds=retry_sleep_seconds if should_retry else 0,
                    )
                    status = "waiting_window" if "window" in message else "no_slots"
                    if _is_transport_error(message):
                        client = None
                    self._mark_job(
                        job, status=status, message=message, persist=not should_retry
                    )
                    if should_retry:
                        self._log(
                            logging.DEBUG,
                            "Sleeping before retry after preview failure.",
                            **self._job_fields(job),
                            attempt=attempt_count,
                            sleep_seconds=retry_sleep_seconds,
                        )
                        time.sleep(retry_sleep_seconds)
                        continue
                    self.record_history(
                        job,
                        triggered_by,
                        False,
                        message,
                        details={"attempts": attempt_count},
                    )
                    return {"ok": False, "message": message, "attempts": attempt_count}

                if dry_run:
                    message = "Preview built successfully."
                    self._log(
                        logging.INFO,
                        "Dry-run preview completed successfully.",
                        **self._job_fields(job),
                        attempt=attempt_count,
                        preview=self._preview_for_log(preview),
                    )
                    self._mark_job(job, status="preview_ready", message=message)
                    self.record_history(
                        job,
                        triggered_by,
                        True,
                        message,
                        details={"attempts": attempt_count},
                    )
                    return {
                        "ok": True,
                        "message": message,
                        "preview": preview,
                        "attempts": attempt_count,
                    }

                self._log(
                    logging.DEBUG,
                    "Submitting confirm order request batch.",
                    **self._job_fields(job),
                    attempt=attempt_count,
                    preview=self._preview_for_log(preview),
                    fallback_candidate_count=len(
                        preview.get("fallback_candidates") or []
                    ),
                )
                try:
                    fallback_candidates = preview.get("fallback_candidates") or []
                    submit_candidates = fallback_candidates or [
                        {
                            "selected_slots": preview["selected_slots"],
                            "selected_spaces": preview.get("selected_spaces", []),
                            "confirm_order_payload": preview["confirm_order_payload"],
                            "total_price": preview["total_price"],
                        }
                    ]
                    last_response: dict[str, Any] | None = None
                    last_message = "Unknown reservation error"
                    for candidate_index, candidate in enumerate(
                        submit_candidates, start=1
                    ):
                        self._log(
                            logging.DEBUG,
                            "Submitting confirm order request.",
                            **self._job_fields(job),
                            attempt=attempt_count,
                            candidate_index=candidate_index,
                            candidate_count=len(submit_candidates),
                            selected_slots=candidate.get("selected_slots", []),
                        )
                        response = client.confirm_personal_order(
                            candidate["confirm_order_payload"]
                        )
                        last_response = response
                        self._log(
                            logging.DEBUG,
                            "Received confirm order response.",
                            **self._job_fields(job),
                            attempt=attempt_count,
                            candidate_index=candidate_index,
                            candidate_count=len(submit_candidates),
                            response=response,
                        )
                        if response.get("code") == 0:
                            successful_preview = dict(preview)
                            successful_preview["selected_slots"] = candidate.get(
                                "selected_slots", []
                            )
                            successful_preview["selected_spaces"] = candidate.get(
                                "selected_spaces", []
                            )
                            successful_preview["confirm_order_payload"] = candidate[
                                "confirm_order_payload"
                            ]
                            successful_preview["total_price"] = candidate.get(
                                "total_price", preview.get("total_price")
                            )
                            order_id = str(response["data"])
                            self._log(
                                logging.INFO,
                                "Reservation order created; requesting payment initialization.",
                                **self._job_fields(job),
                                attempt=attempt_count,
                                candidate_index=candidate_index,
                                order_id=order_id,
                            )
                            payment = client.create_payment(order_id)
                            self._log(
                                logging.DEBUG,
                                "Received payment initialization response.",
                                **self._job_fields(job),
                                attempt=attempt_count,
                                candidate_index=candidate_index,
                                order_id=order_id,
                                payment=payment,
                            )
                            message = (
                                f"Reservation order created successfully: {order_id}"
                            )
                            self._mark_job(
                                job,
                                status="success",
                                message=message,
                                order_id=order_id,
                                payment=payment,
                                success=True,
                            )
                            self.record_history(
                                job,
                                triggered_by,
                                True,
                                message,
                                details={
                                    "attempts": attempt_count,
                                    "order_id": order_id,
                                },
                            )
                            notification = self._send_booking_notification(
                                job,
                                order_id=order_id,
                                payment=payment,
                                preview=successful_preview,
                            )
                            return {
                                "ok": True,
                                "message": message,
                                "order_id": order_id,
                                "payment": payment,
                                "notification": notification,
                                "preview": successful_preview,
                                "attempts": attempt_count,
                            }

                        last_message = response.get("msg", "Unknown reservation error")
                        if response.get("code") == 1002:
                            self._log(
                                logging.WARNING,
                                "Reservation attempt requires captcha verification.",
                                **self._job_fields(job),
                                attempt=attempt_count,
                                candidate_index=candidate_index,
                                response=response,
                            )
                            self._mark_job(
                                job, status="captcha_required", message=last_message
                            )
                            self.record_history(
                                job, triggered_by, False, last_message, details=response
                            )
                            return {
                                "ok": False,
                                "message": last_message,
                                "response": response,
                            }

                        if candidate_index < len(submit_candidates):
                            self._log(
                                logging.DEBUG,
                                "Fallback candidate failed; trying next candidate from the same slot snapshot.",
                                **self._job_fields(job),
                                attempt=attempt_count,
                                candidate_index=candidate_index,
                                error=last_message,
                            )
                except SportsAPIError as exc:
                    message = str(exc)
                    should_retry = time.time() + retry_sleep_seconds <= deadline
                    self._log(
                        logging.WARNING,
                        "Reservation submit request failed.",
                        **self._job_fields(job),
                        attempt=attempt_count,
                        error=message,
                        should_retry=should_retry,
                        retry_sleep_seconds=retry_sleep_seconds if should_retry else 0,
                    )
                    if _is_transport_error(message):
                        client = None
                    self._mark_job(
                        job,
                        status="submit_failed",
                        message=message,
                        persist=not should_retry,
                    )
                    if should_retry:
                        self._log(
                            logging.DEBUG,
                            "Sleeping before retry after submit request failure.",
                            **self._job_fields(job),
                            attempt=attempt_count,
                            sleep_seconds=retry_sleep_seconds,
                        )
                        time.sleep(retry_sleep_seconds)
                        continue
                    self.record_history(
                        job,
                        triggered_by,
                        False,
                        message,
                        details={"attempts": attempt_count},
                    )
                    return {"ok": False, "message": message, "attempts": attempt_count}

                message = last_message
                response = last_response or {}
                should_retry = time.time() + retry_sleep_seconds <= deadline
                self._log(
                    logging.WARNING,
                    "Reservation submit batch failed.",
                    **self._job_fields(job),
                    attempt=attempt_count,
                    error=message,
                    response=response,
                    candidate_count=len(submit_candidates),
                    should_retry=should_retry,
                    retry_sleep_seconds=retry_sleep_seconds if should_retry else 0,
                )
                self._mark_job(
                    job,
                    status="submit_failed",
                    message=message,
                    persist=not should_retry,
                )
                if should_retry:
                    self._log(
                        logging.DEBUG,
                        "Sleeping before retry after submit failure.",
                        **self._job_fields(job),
                        attempt=attempt_count,
                        sleep_seconds=retry_sleep_seconds,
                    )
                    time.sleep(retry_sleep_seconds)
                    continue
                self.record_history(job, triggered_by, False, message, details=response)
                return {"ok": False, "message": message, "response": response}

    def _send_booking_notification(
        self,
        job: ReservationJob,
        *,
        order_id: str,
        payment: dict[str, Any],
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.notifier:
            self._log(
                logging.DEBUG,
                "Skipping notification because ntfy is not configured.",
                **self._job_fields(job),
            )
            return {"configured": False, "sent": False}

        selected_slots = preview.get("selected_slots", [])
        slot_lines = "\n".join(
            f"- {slot['field_name']} {slot['time_slot']} (Yuan {slot['price']})"
            for slot in selected_slots
        )
        payment_url = payment.get("payment_url")
        message_lines = [
            f"Venue: {job.venue_name}",
            f"Motion: {job.motion}",
            f"Date: {job.target_date}",
            f"Job: {job.name}",
            f"Order ID: {order_id}",
            f"Total: Yuan {preview.get('total_price', '?')}",
        ]
        if slot_lines:
            message_lines.extend(["Slots:", slot_lines])
        if payment_url:
            message_lines.append(f"Payment URL: {payment_url}")

        try:
            self._log(
                logging.DEBUG,
                "Sending ntfy notification for successful reservation.",
                **self._job_fields(job),
                order_id=order_id,
                payment_url=payment_url,
            )
            result = self.notifier.send(
                "\n".join(message_lines),
                title=f"SJTU sports booked: {job.venue_name}",
                tags=["sports", "reservation"],
                click=payment_url,
            )
        except Exception as exc:  # pragma: no cover - network failure path
            self._log(
                logging.WARNING,
                "Failed to send ntfy notification.",
                **self._job_fields(job),
                order_id=order_id,
                error=str(exc),
            )
            self.record_history(
                job, "notification", False, f"ntfy notification failed: {exc}"
            )
            return {"configured": True, "sent": False, "error": str(exc)}

        details = {"topic": result.topic, "status_code": result.status_code}
        if result.message_id:
            details["message_id"] = result.message_id
        self._log(
            logging.INFO,
            "ntfy notification sent.",
            **self._job_fields(job),
            order_id=order_id,
            topic=result.topic,
            status_code=result.status_code,
            message_id=result.message_id,
        )
        self.record_history(
            job, "notification", True, "ntfy notification sent.", details=details
        )
        return {
            "configured": True,
            "sent": True,
            "topic": result.topic,
            "status_code": result.status_code,
            "message_id": result.message_id,
        }

    def run_scheduled_jobs(self) -> None:
        today = _now().date()
        self._log(
            logging.INFO,
            "Running scheduled noon reservation scan.",
            today=today.isoformat(),
        )
        for job in self.list_jobs():
            if not job.enabled or job.job_type != JOB_TYPE_TARGET_DATE:
                continue
            if job.run_on_date:
                scheduled_date = _parse_date(job.run_on_date)
                if scheduled_date > today:
                    self._log(
                        logging.DEBUG,
                        "Skipping target-date job because scheduled noon is still in the future.",
                        **self._job_fields(job),
                    )
                    continue
                if scheduled_date < today:
                    if job.last_status in {"idle", "waiting_schedule"}:
                        message = (
                            f"Scheduled noon on {job.run_on_date} has already passed."
                        )
                        self._mark_job(job, status="missed_schedule", message=message)
                        self.record_history(job, "schedule", False, message)
                        self._log(
                            logging.WARNING,
                            "Scheduled noon already passed for target-date job.",
                            **self._job_fields(job),
                        )
                    continue
            try:
                self._log(
                    logging.INFO,
                    "Dispatching scheduled target-date reservation job.",
                    **self._job_fields(job),
                )
                self.run_job(job.job_id, dry_run=False, triggered_by="schedule")
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.exception(
                    _format_log_message(
                        "Scheduled sports job failed.",
                        **self._job_fields(job),
                        error=str(exc),
                    )
                )


def create_dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>体育场馆预约守护进程</title>
  <style>
:root {
    --bg: #0a0a0f;
    --card-bg: #1a1a1a;
    --text: #eee;
    --sub-text: #888;
    --accent: #3fb950;
    --warn: #d29922;
    --error: #f85149;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font);
    background: var(--bg);
    color: var(--text);
    padding: 2rem 1rem;
    line-height: 1.5;
}
.wrap { max-width: 540px; margin: 0 auto; }

/* Header & Live Indicator */
header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 2rem; }
h1 { font-size: 1.25rem; font-weight: 600; letter-spacing: -0.5px; }
.status-dot {
    font-size: 0.75rem; color: var(--accent); display: flex; align-items: center; gap: 6px;
    background: rgba(63, 185, 80, 0.1); padding: 4px 10px; border-radius: 20px;
}
.pulse { display: inline-block; width: 6px; height: 6px; background: currentColor; border-radius: 50%; animation: blink 2s infinite; }

/* Sections */
h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; color: var(--sub-text); margin: 0 0 1rem 0; font-weight: 600; }
.section { margin-bottom: 2.5rem; }

/* Hero Status Card */
.hero {
    background: var(--card-bg);
    padding: 1.5rem;
    border-radius: 12px;
    text-align: center;
}
.hero-status { font-size: 1.1rem; margin-bottom: 0.25rem; font-weight: bold; }
.hero-sub { color: var(--sub-text); font-size: 0.8rem; font-family: monospace; }

/* Badges row */
.badges { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 0.75rem; }
.badge {
    font-size: 0.7rem; padding: 3px 8px; border-radius: 12px;
    background: rgba(63, 185, 80, 0.1); color: var(--accent); font-family: monospace;
}

/* Form */
.form-section { margin-bottom: 2.5rem; }
label { display: block; font-size: 0.8rem; font-weight: 600; color: var(--sub-text); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
input, select {
    width: 100%; border: 1px solid #333; border-radius: 8px; padding: 10px;
    background: var(--card-bg); color: var(--text); font: inherit; font-size: 0.9rem;
    outline: none; -webkit-appearance: none; appearance: none;
}
select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 10px center; padding-right: 28px;
}
input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent);
    -webkit-appearance: auto;
    appearance: auto;
    padding: 0;
    border: none;
}
input:focus, select:focus { border-color: var(--accent); }
.field-row { margin-bottom: 0.75rem; }
.field-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.inline-row { display: flex; gap: 8px; align-items: end; }
.inline-row input { flex: 1; }

/* Buttons */
button {
    border: 0; border-radius: 8px; padding: 8px 14px; font: inherit; font-size: 0.8rem;
    font-weight: 600; cursor: pointer; transition: opacity 0.15s;
}
button:hover { opacity: 0.85; }
.btn-primary { background: var(--accent); color: #000; }
.btn-ghost { background: transparent; color: var(--sub-text); border: 1px solid #333; }
.btn-warn { background: rgba(210, 153, 34, 0.15); color: var(--warn); border: 1px solid rgba(210, 153, 34, 0.3); }
.btn-danger { background: rgba(248, 81, 73, 0.12); color: var(--error); border: 1px solid rgba(248, 81, 73, 0.25); }
.btn-run { background: rgba(63, 185, 80, 0.12); color: var(--accent); border: 1px solid rgba(63, 185, 80, 0.25); }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 0.75rem; }

/* Time-slot checkboxes */
.slot-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.slot-grid label {
    display: inline-flex; align-items: center; gap: 5px; font-size: 0.8rem; font-weight: 400;
    color: var(--text); text-transform: none; letter-spacing: 0; margin: 0; padding: 5px 10px;
    border-radius: 6px; background: rgba(255,255,255,0.04); cursor: pointer; white-space: nowrap;
}
.slot-grid label:hover { background: rgba(255,255,255,0.08); }

/* Info box (availability) */
.info-box {
    border-radius: 8px; padding: 12px; margin-top: 0.75rem;
    background: rgba(255,255,255,0.04); color: var(--sub-text); font-size: 0.85rem; line-height: 1.6;
}
.hidden { display: none !important; }

/* Jobs List */
.job {
    background: var(--card-bg); border-radius: 10px; padding: 1rem; margin-bottom: 0.75rem;
}
.job-head { display: flex; justify-content: space-between; align-items: start; gap: 8px; }
.job-name { font-size: 1rem; font-weight: 600; margin: 0; }
.job-meta { color: var(--sub-text); font-size: 0.8rem; margin-top: 4px; line-height: 1.6; }
.status-badge {
    font-size: 0.65rem; padding: 3px 8px; border-radius: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; flex-shrink: 0;
}
.st-success, .st-idle { background: rgba(63, 185, 80, 0.12); color: var(--accent); }
.st-waiting_window, .st-no_slots, .st-preview_ready { background: rgba(210, 153, 34, 0.12); color: var(--warn); }
.st-submit_failed, .st-expired, .st-captcha_required { background: rgba(248, 81, 73, 0.12); color: var(--error); }
.job-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 0.75rem; }
.job-actions button { font-size: 0.75rem; padding: 5px 10px; }

/* History List */
.list { display: flex; flex-direction: column; gap: 0.75rem; }
.hist-item { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.85rem; gap: 8px; }
.hist-msg { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hist-time { color: var(--sub-text); font-size: 0.7rem; font-family: monospace; flex-shrink: 0; }
.dot { margin-right: 6px; font-weight: bold; }
.st-ok { color: var(--accent); }
.st-fail { color: var(--error); }

/* Availability */
.avail-day { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 10px; margin-bottom: 8px; }
.avail-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; font-size: 0.85rem; }
.avail-title { font-weight: 600; }
.avail-slots { display: flex; flex-wrap: wrap; gap: 6px; }
.avail-pill {
    font-size: 0.75rem; padding: 3px 8px; border-radius: 10px;
    background: rgba(63, 185, 80, 0.08); color: var(--accent);
}

/* Footer */
.meta-footer {
    border-top: 1px solid #222; padding-top: 1.5rem; margin-top: 1rem;
    font-size: 0.7rem; color: var(--sub-text); display: flex; justify-content: space-between;
}
.empty { color: var(--sub-text); font-style: italic; font-size: 0.85rem; }

@keyframes blink { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
  </style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>交我约</h1>
        <div class="status-dot"><span class="pulse"></span> 守护进程</div>
    </header>

    <!-- Status -->
    <div class="section">
        <h2>状态</h2>
        <div class="hero">
            <div class="hero-status" id="heroStatus">加载中…</div>
            <div class="hero-sub" id="heroSub"></div>
            <div class="badges">
                <span class="badge" id="authMode">…</span>
                <span class="badge" id="nextRun">…</span>
            </div>
        </div>
    </div>

    <!-- Create Job -->
    <div class="form-section">
        <h2>新建任务</h2>
        <div class="field-row">
            <label for="jobName">任务名称</label>
            <input id="jobName" placeholder="例如：周一乒乓球">
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="jobType">模式</label>
                <select id="jobType" onchange="toggleJobMode()">
                    <option value="target_date">指定日期</option>
                    <option value="cron">定时监控</option>
                </select>
            </div>
            <div class="field-row" id="targetDateRow">
                <label for="targetDate">日期</label>
                <input id="targetDate" type="date">
            </div>
        </div>
        <div class="field-row" id="runOnDateRow">
            <label for="runOnDate">运行日期（当天中午）</label>
            <input id="runOnDate" type="date">
        </div>
        <div class="field-row">
            <label for="venueSearch">场馆搜索</label>
            <div class="inline-row">
                <input id="venueSearch" placeholder="搜索场馆名称">
                <button type="button" class="btn-ghost" onclick="searchVenues()">搜索</button>
            </div>
        </div>
        <div class="field-row">
            <label for="venueSelect">场馆</label>
            <select id="venueSelect" onchange="loadVenueDetail()">
                <option value="">选择场馆</option>
            </select>
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="motionSelect">运动类型</label>
                <select id="motionSelect" onchange="loadAvailability()">
                    <option value="">选择运动类型</option>
                </select>
            </div>
            <div class="field-row" id="retryWindowRow">
                <label for="retryWindow">重试窗口 (秒)</label>
                <input id="retryWindow" type="number" min="10" value="180">
            </div>
        </div>
        <div class="field-pair hidden" id="cronWindowRows">
            <div class="field-row">
                <label for="windowStartDays">窗口开始 (距今天数)</label>
                <input id="windowStartDays" type="number" min="0" value="0">
            </div>
            <div class="field-row">
                <label for="windowEndDays">窗口结束 (距今天数)</label>
                <input id="windowEndDays" type="number" min="0" value="7">
            </div>
        </div>
        <div class="field-pair hidden" id="cronTimingRows">
            <div class="field-row">
                <label for="cronIntervalMinutes">检查间隔 (分钟)</label>
                <input id="cronIntervalMinutes" type="number" min="1" value="10">
            </div>
            <div class="field-row">
                <label for="redeemDeadlineHours">下单截止 (提前小时数)</label>
                <input id="redeemDeadlineHours" type="number" min="0" value="2">
            </div>
        </div>
        <div class="field-pair">
            <div class="field-row">
                <label for="preferredFields">偏好场地</label>
                <input id="preferredFields" placeholder="按优先级填写，如：场地1,场地2,场地3">
            </div>
            <div class="field-row" id="retryIntervalRow">
                <label for="retryInterval">重试间隔 (秒)</label>
                <input id="retryInterval" type="number" min="0.2" step="0.1" value="0.2">
            </div>
        </div>
        <div class="field-row">
            <label for="slotSelectionMode">时间段策略</label>
            <select id="slotSelectionMode">
                <option value="first_available">按优先级抢一个场地时段</option>
                <option value="all_required">必须同时满足所有已选时间段</option>
            </select>
        </div>
        <div class="field-row">
            <label>时间段</label>
            <div class="slot-grid" id="timeSlotGrid"></div>
        </div>
        <div class="btn-row">
            <button type="button" class="btn-primary" onclick="createJob()">保存任务</button>
            <button type="button" class="btn-ghost" onclick="refreshStatus()">刷新</button>
        </div>
        <div id="modeHelp" class="info-box">指定日期模式默认会在下一个到来的中午运行，你也可以手动指定具体哪一天中午执行。时间段策略选择“按优先级抢一个场地时段”时，会按你勾选时间段的先后顺序，以及偏好场地的填写顺序，从同一次可用性检查结果里挑出第一个可下单组合，只提交一个场地，适合“每天同项目只能下一单”的规则。定时监控模式将每隔几分钟扫描配置的日期窗口内的新释放场地，并在下单截止前预订。</div>
        <div id="availabilityBox" class="info-box">搜索场馆以查看可用性。</div>
    </div>

    <!-- Jobs -->
    <div class="section">
        <h2>任务列表</h2>
        <div id="jobs"></div>
    </div>

    <!-- History -->
    <div class="section">
        <h2>最近活动</h2>
        <div id="history" class="list"></div>
    </div>

    <div class="meta-footer">
        <div>体育场馆预约守护进程</div>
        <div>每15秒自动刷新</div>
    </div>
</div>

<script>
const TIME_SLOTS = {{ time_slots | safe }};
let cachedVenues = [];
let timeSlotPriority = [];

function esc(v) {
    return String(v ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function selectedTimeSlots() {
    const checked = new Set(
        [...document.querySelectorAll('input[name="timeSlot"]:checked')].map(i => i.value)
    );
    timeSlotPriority = timeSlotPriority.filter(value => checked.has(value));
    return [...timeSlotPriority];
}

function updateTimeSlotPriority(slot, checked) {
    timeSlotPriority = timeSlotPriority.filter(value => value !== slot);
    if (checked) timeSlotPriority.push(slot);
}

function formatLocalDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
}

function defaultRunOnDateValue() {
    const now = new Date();
    const noonPassed =
        now.getHours() > 12 ||
        (now.getHours() === 12 && (now.getMinutes() > 0 || now.getSeconds() > 0 || now.getMilliseconds() > 0));
    const runDate = new Date(now);
    if (noonPassed) runDate.setDate(runDate.getDate() + 1);
    return formatLocalDate(runDate);
}

function resetRunOnDateDefault(force = false) {
    const input = document.getElementById("runOnDate");
    if (force || !input.value) {
        input.value = defaultRunOnDateValue();
    }
}

function toggleJobMode() {
    const mode = document.getElementById("jobType").value;
    const isCron = mode === "cron";
    document.getElementById("targetDateRow").classList.toggle("hidden", isCron);
    document.getElementById("runOnDateRow").classList.toggle("hidden", isCron);
    document.getElementById("retryWindowRow").classList.toggle("hidden", isCron);
    document.getElementById("retryIntervalRow").classList.toggle("hidden", isCron);
    document.getElementById("cronWindowRows").classList.toggle("hidden", !isCron);
    document.getElementById("cronTimingRows").classList.toggle("hidden", !isCron);
    if (!isCron) resetRunOnDateDefault();
}

function renderTimeSlots() {
    document.getElementById("timeSlotGrid").innerHTML = TIME_SLOTS.map(s =>
        `<label><input type="checkbox" name="timeSlot" value="${s}" onchange="updateTimeSlotPriority('${s}', this.checked)"> ${s}</label>`
    ).join("");
}

async function api(path, opts = {}) {
    const r = await fetch(path, { headers: {"Content-Type":"application/json"}, ...opts });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || d.message || "Request failed");
    return d;
}

async function searchVenues() {
    const q = document.getElementById("venueSearch").value.trim();
    const d = await api(`/api/catalog/venues?search=${encodeURIComponent(q)}`);
    cachedVenues = d.venues || [];
    document.getElementById("venueSelect").innerHTML =
        `<option value="">选择场馆</option>` +
        cachedVenues.map(v => `<option value="${v.venueId}">${esc(v.venueName)} · ${esc(v.campusName||"")}</option>`).join("");
}

async function loadVenueDetail() {
    const vid = document.getElementById("venueSelect").value;
    const ms = document.getElementById("motionSelect");
    ms.innerHTML = `<option value="">选择运动类型</option>`;
    if (!vid) return;
    const d = await api(`/api/catalog/venues/${vid}`);
    const v = d.venue;
    ms.innerHTML += v.motion_types.map(m => `<option value="${m.name}">${esc(m.name)}</option>`).join("");
    document.getElementById("availabilityBox").innerHTML =
        `<strong>${esc(v.venue_name)}</strong><br>${esc(v.campus_name)} · ${esc(v.open_time)}<br>${esc(v.venue_mobile)}`;
}

async function loadAvailability() {
    const vid = document.getElementById("venueSelect").value;
    const mot = document.getElementById("motionSelect").value;
    if (!vid || !mot) return;
    const d = await api(`/api/catalog/venues/${vid}/availability?motion=${encodeURIComponent(mot)}`);
    const html = d.availability.map(item => {
        const slots = item.selectable_slots || [];
        const pills = slots.length
            ? `<div class="avail-slots">${slots.map(s =>
                `<span class="avail-pill">${esc(s.field_name)} · ${esc(s.time_slot)} · ¥${esc(s.price)}</span>`
              ).join("")}</div>`
            : `<div class="empty">没有可选时间段。</div>`;
        return `<div class="avail-day">
            <div class="avail-head"><span class="avail-title">${esc(item.date)} · ${esc(item.view_str)}</span><span style="color:var(--sub-text);font-size:0.75rem">${item.selectable_count} 个空余</span></div>
            ${pills}</div>`;
    }).join("");
    document.getElementById("availabilityBox").innerHTML = html || `<div class="empty">没有可见日期。</div>`;
}

async function createJob() {
    const sel = document.getElementById("venueSelect");
    const jobType = document.getElementById("jobType").value;
    const payload = {
        job_type: jobType,
        name: document.getElementById("jobName").value.trim(),
        venue_id: sel.value,
        venue_name: sel.selectedOptions[0]?.textContent?.split(" · ")[0] || "",
        motion: document.getElementById("motionSelect").value,
        target_date: jobType === "target_date" ? document.getElementById("targetDate").value : "",
        run_on_date: jobType === "target_date" ? document.getElementById("runOnDate").value : "",
        slot_selection_mode: document.getElementById("slotSelectionMode").value,
        preferred_fields: document.getElementById("preferredFields").value,
        time_slots: selectedTimeSlots(),
        retry_window_seconds: Number(document.getElementById("retryWindow").value || 180),
        retry_interval_seconds: Number(document.getElementById("retryInterval").value || 0.2),
        cron_interval_minutes: Number(document.getElementById("cronIntervalMinutes").value || 10),
        window_start_days: Number(document.getElementById("windowStartDays").value || 0),
        window_end_days: Number(document.getElementById("windowEndDays").value || 7),
        redeem_deadline_hours: Number(document.getElementById("redeemDeadlineHours").value || 2),
        enabled: true,
    };
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    document.getElementById("jobName").value = "";
    document.getElementById("preferredFields").value = "";
    resetRunOnDateDefault(true);
    document.querySelectorAll('input[name="timeSlot"]').forEach(i => { i.checked = false; });
    timeSlotPriority = [];
    await refreshStatus();
}

function slotSelectionModeLabel(mode) {
    if (mode === "first_available") return "按优先级抢一个";
    return "全部时间段都要满足";
}

async function toggleJob(id, en) {
    await api(`/api/jobs/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: en }) });
    await refreshStatus();
}

async function deleteJob(id) {
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    await refreshStatus();
}

async function runJob(id, dry) {
    const d = await api(`/api/jobs/${id}/run`, { method: "POST", body: JSON.stringify({ dry_run: dry }) });
    alert(d.result?.message || d.result?.order_id || "完成。");
    await refreshStatus();
}

function statusClass(s) {
    if (["success","idle"].includes(s)) return "st-success";
    if (["waiting_window","waiting_schedule","no_slots","preview_ready"].includes(s)) return "st-waiting_window";
    return "st-submit_failed";
}

function renderJobs(jobs) {
    const el = document.getElementById("jobs");
    if (!jobs.length) { el.innerHTML = `<div class="empty">暂无任务。</div>`; return; }
    el.innerHTML = jobs.map(j => `
        <div class="job">
            <div class="job-head">
                <div>
                    <div class="job-name">${esc(j.name)}</div>
                    <div class="job-meta">
                        ${esc(j.venue_name)} · ${esc(j.motion)}<br>
                        ${j.job_type === "cron"
                            ? `定时检查 每 ${esc(j.cron_interval_minutes)} 分钟 · 天数 +${esc(j.window_start_days)} 至 +${esc(j.window_end_days)}<br>下单截止: 提前 ${esc(j.redeem_deadline_hours)} 小时 · 时间段: ${esc(j.time_slots.join(", "))}`
                            : `${esc(j.target_date)} · ${esc(j.time_slots.join(", "))}<br>计划运行: ${esc(j.run_on_date ? `${j.run_on_date} 12:00` : "每天中午（旧任务）")} · 重试窗口: ${esc(j.retry_window_seconds)} 秒`
                        }<br>
                        策略: ${esc(slotSelectionModeLabel(j.slot_selection_mode))}<br>
                        场地: ${esc(j.preferred_fields.join(", ") || "任意")}
                    </div>
                </div>
                <span class="status-badge ${statusClass(j.last_status)}">${esc(j.last_status)}</span>
            </div>
            <div class="job-meta" style="margin-top:8px">
                ${esc(j.last_message || "—")}<br>
                检查时间: ${esc(j.last_checked_at || "从未")} · 订单: ${esc(j.last_order_id || "—")}
            </div>
            <div class="job-actions">
                <button class="btn-ghost" onclick="runJob('${j.job_id}',true)">预览</button>
                <button class="btn-run" onclick="runJob('${j.job_id}',false)">运行</button>
                <button class="btn-warn" onclick="toggleJob('${j.job_id}',${j.enabled?"false":"true"})">${j.enabled?"禁用":"启用"}</button>
                <button class="btn-danger" onclick="deleteJob('${j.job_id}')">删除</button>
            </div>
        </div>
    `).join("");
}

function renderHistory(items) {
    const el = document.getElementById("history");
    if (!items.length) { el.innerHTML = `<div class="empty">暂无活动。</div>`; return; }
    el.innerHTML = items.map(i => `
        <div class="hist-item">
            <div class="hist-msg">
                <span class="dot ${i.success?"st-ok":"st-fail"}">${i.success?"•":"×"}</span>
                <span style="${i.success?"":"color:var(--error)"}">${esc(i.job_name)}: ${esc(i.message)}</span>
            </div>
            <div class="hist-time">${esc((i.timestamp||"").substring(11,19))}</div>
        </div>
    `).join("");
}

async function refreshStatus() {
    const d = await api("/api/status");
    const jobCount = (d.jobs||[]).length;
    const enabled = (d.jobs||[]).filter(j=>j.enabled).length;
    document.getElementById("heroStatus").textContent = `${jobCount} 个任务, ${enabled} 个活跃`;
    document.getElementById("heroSub").textContent = d.next_run_at ? `下次运行: ${d.next_run_at}` : "暂无计划运行";
    document.getElementById("authMode").textContent = d.auth_mode;
    document.getElementById("nextRun").textContent = d.next_run_at ? new Date(d.next_run_at).toLocaleTimeString() : "—";
    renderJobs(d.jobs || []);
    renderHistory(d.history || []);
}

renderTimeSlots();
resetRunOnDateDefault(true);
toggleJobMode();
refreshStatus();
setInterval(refreshStatus, 15000);
</script>
</body>
</html>"""


def create_app(daemon: SportsReservationDaemon) -> Flask:
    app = Flask(__name__)
    dashboard_html = create_dashboard_html()

    @app.route("/")
    def dashboard():
        return render_template_string(
            dashboard_html, time_slots=json.dumps(SPORTS_TIME_SLOTS)
        )

    @app.route("/api/status")
    def status():
        return jsonify(daemon.service_status())

    @app.route("/api/catalog/venues")
    def catalog_venues():
        search = request.args.get("search", "")
        try:
            venues = daemon.list_venues(search)
            return jsonify({"venues": venues})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/catalog/venues/<venue_id>")
    def catalog_venue_detail(venue_id: str):
        try:
            venue = daemon.get_venue_detail(venue_id)
            return jsonify({"venue": venue})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/catalog/venues/<venue_id>/availability")
    def catalog_availability(venue_id: str):
        motion = request.args.get("motion", "")
        if not motion:
            return jsonify({"error": "motion is required"}), 400
        try:
            availability = daemon.get_availability(venue_id, motion)
            return jsonify({"availability": availability})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs", methods=["GET", "POST"])
    def jobs():
        if request.method == "GET":
            return jsonify({"jobs": [job.to_dict() for job in daemon.list_jobs()]})
        try:
            job = daemon.create_job(request.get_json(force=True))
            return jsonify({"job": job.to_dict()}), 201
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs/<job_id>", methods=["PATCH", "DELETE"])
    def job_detail(job_id: str):
        try:
            if request.method == "DELETE":
                daemon.delete_job(job_id)
                return jsonify({"ok": True})
            job = daemon.update_job(job_id, request.get_json(force=True))
            return jsonify({"job": job.to_dict()})
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs/<job_id>/run", methods=["POST"])
    def run_job(job_id: str):
        try:
            payload = request.get_json(force=True, silent=True) or {}
            result = daemon.run_job(
                job_id,
                dry_run=bool(payload.get("dry_run", False)),
                triggered_by="manual",
            )
            return jsonify({"result": result})
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.after_request
    def log_request(response):
        daemon._log(
            logging.DEBUG,
            "Handled HTTP request.",
            method=request.method,
            path=request.path,
            status_code=response.status_code,
            client_ip=get_client_ip(),
        )
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sports reservation daemon.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to listen on (default: 127.0.0.1)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=5003, help="Port to listen on (default: 5003)"
    )
    parser.add_argument(
        "--from-browser",
        action="store_true",
        help="Reuse the current Playwright browser session instead of credentials.json.",
    )
    args = parser.parse_args()

    daemon = SportsReservationDaemon(use_browser_session=args.from_browser)
    daemon.start()

    app = create_app(daemon)
    dashboard_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    print(f"Starting Sports Reservation Daemon on {args.host}:{args.port}")
    print(f"Dashboard: http://{dashboard_host}:{args.port}/")
    print(f"Auth mode: {'browser' if args.from_browser else 'credentials'}")
    app.run(debug=False, host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
