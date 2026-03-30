"""Sports reservation daemon with a web dashboard and noon scheduler."""

from __future__ import annotations

import argparse
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
    SPORTS_TIME_SLOTS,
    DateOption,
    FieldSlot,
    MotionType,
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


logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO)


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_date(value: str) -> datetime.date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _normalize_time_slots(values: list[str] | tuple[str, ...] | str) -> list[str]:
    if isinstance(values, str):
        values = [item.strip() for item in values.split(",")]
    seen: set[str] = set()
    normalized = [slot for slot in SPORTS_TIME_SLOTS if slot in values and slot not in seen and not seen.add(slot)]
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
    preferred_fields: list[str] = field(default_factory=list)
    enabled: bool = True
    retry_window_seconds: int = 180
    retry_interval_seconds: int = 5
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
        return cls(
            job_id=payload["job_id"],
            name=payload["name"],
            venue_id=payload["venue_id"],
            venue_name=payload.get("venue_name", payload["venue_id"]),
            motion=payload["motion"],
            job_type=payload.get("job_type", JOB_TYPE_TARGET_DATE),
            target_date=payload.get("target_date", ""),
            time_slots=_normalize_time_slots(payload.get("time_slots", [])),
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=int(payload.get("retry_window_seconds", 180)),
            retry_interval_seconds=int(payload.get("retry_interval_seconds", 5)),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
            cron_interval_minutes=max(1, int(payload.get("cron_interval_minutes", 10))),
            window_start_days=max(0, int(payload.get("window_start_days", 0))),
            window_end_days=max(0, int(payload.get("window_end_days", 7))),
            redeem_deadline_hours=max(0, int(payload.get("redeem_deadline_hours", 2))),
            created_at=payload.get("created_at", _now_iso()),
            updated_at=payload.get("updated_at", _now_iso()),
            last_checked_at=payload.get("last_checked_at"),
            last_status=payload.get("last_status", "idle"),
            last_message=payload.get("last_message", ""),
            last_order_id=payload.get("last_order_id"),
            last_payment_url=payload.get("last_payment_url"),
            last_payment_init=payload.get("last_payment_init"),
            success_at=payload.get("success_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SportsReservationDaemon:
    def __init__(
        self,
        *,
        use_browser_session: bool = False,
        jobs_file: Path = JOBS_FILE,
    ):
        self.use_browser_session = use_browser_session
        self.jobs_file = jobs_file
        self.jobs_lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.history = deque(maxlen=100)
        self.scheduler = BackgroundScheduler(timezone=TIMEZONE)
        self.scheduler.add_job(
            self.run_scheduled_jobs,
            trigger=CronTrigger(hour=12, minute=0, timezone=TIMEZONE),
            id="sports-noon-run",
            replace_existing=True,
        )
        self.jobs: dict[str, ReservationJob] = self._load_jobs()
        self.notifier = NtfyNotifier.from_config(credentials.ntfy_config)
        self._sync_cron_jobs()

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def create_client(self) -> SportsReservationClient:
        jac_login = JACLogin(credentials.username or "", credentials.password or "")
        client = SportsReservationClient(jac_login)
        if self.use_browser_session:
            client.load_playwright_browser_session()
            return client
        if not credentials.username or not credentials.password:
            raise SportsAPIError(
                "Daemon mode needs credentials.json or env credentials unless it is started with --from-browser."
            )
        client.login()
        return client

    def _load_jobs(self) -> dict[str, ReservationJob]:
        if not self.jobs_file.exists():
            return {}
        with self.jobs_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        jobs = payload.get("jobs", [])
        return {item["job_id"]: ReservationJob.from_dict(item) for item in jobs}

    def _save_jobs(self) -> None:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now_iso(),
            "jobs": [job.to_dict() for job in self.list_jobs()],
        }
        with self.jobs_file.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def list_jobs(self) -> list[ReservationJob]:
        with self.jobs_lock:
            return sorted(
                self.jobs.values(),
                key=lambda job: (job.target_date or "9999-12-31", job.name, job.created_at),
            )

    @staticmethod
    def _validate_job_type(job_type: str) -> str:
        normalized = (job_type or JOB_TYPE_TARGET_DATE).strip()
        if normalized not in {JOB_TYPE_TARGET_DATE, JOB_TYPE_CRON}:
            raise ValueError(f"Unknown job_type: {job_type}")
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
                trigger=IntervalTrigger(minutes=job.cron_interval_minutes, timezone=TIMEZONE),
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

    def get_job(self, job_id: str) -> ReservationJob:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return job

    def create_job(self, payload: dict[str, Any]) -> ReservationJob:
        time_slots = _normalize_time_slots(payload.get("time_slots", []))
        if not time_slots:
            raise ValueError("Choose at least one time slot.")

        job_type = self._validate_job_type(payload.get("job_type", JOB_TYPE_TARGET_DATE))
        target_date = (payload.get("target_date") or "").strip()
        if job_type == JOB_TYPE_TARGET_DATE:
            if not target_date:
                raise ValueError("Choose a target date.")
            _parse_date(target_date)

        window_start_days = max(0, int(payload.get("window_start_days", 0)))
        window_end_days = max(0, int(payload.get("window_end_days", 7)))
        if window_end_days < window_start_days:
            raise ValueError("window_end_days must be greater than or equal to window_start_days.")

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
            time_slots=time_slots,
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=max(10, int(payload.get("retry_window_seconds", 180))),
            retry_interval_seconds=max(1, int(payload.get("retry_interval_seconds", 5))),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
            cron_interval_minutes=max(1, int(payload.get("cron_interval_minutes", 10))),
            window_start_days=window_start_days,
            window_end_days=window_end_days,
            redeem_deadline_hours=max(0, int(payload.get("redeem_deadline_hours", 2))),
        )
        with self.jobs_lock:
            self.jobs[job.job_id] = job
            self._save_jobs()
            self._sync_cron_jobs()
        self.record_history(job, "created", True, "Reservation job created.")
        return job

    def update_job(self, job_id: str, payload: dict[str, Any]) -> ReservationJob:
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
            if "target_date" in payload:
                target_date = (payload["target_date"] or "").strip()
                if job.job_type == JOB_TYPE_TARGET_DATE:
                    if not target_date:
                        raise ValueError("Choose a target date.")
                    _parse_date(target_date)
                job.target_date = target_date
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
                job.retry_interval_seconds = max(1, int(payload["retry_interval_seconds"]))
            if "auto_disable_on_success" in payload:
                job.auto_disable_on_success = bool(payload["auto_disable_on_success"])
            if "cron_interval_minutes" in payload:
                job.cron_interval_minutes = max(1, int(payload["cron_interval_minutes"]))
            if "window_start_days" in payload:
                job.window_start_days = max(0, int(payload["window_start_days"]))
            if "window_end_days" in payload:
                job.window_end_days = max(0, int(payload["window_end_days"]))
            if "redeem_deadline_hours" in payload:
                job.redeem_deadline_hours = max(0, int(payload["redeem_deadline_hours"]))

            if job.job_type == JOB_TYPE_TARGET_DATE and not job.target_date:
                raise ValueError("Choose a target date.")
            if job.window_end_days < job.window_start_days:
                raise ValueError("window_end_days must be greater than or equal to window_start_days.")
            job.updated_at = _now_iso()
            self._save_jobs()
            self._sync_cron_jobs()
            return job

    def delete_job(self, job_id: str) -> None:
        with self.jobs_lock:
            job = self.jobs.pop(job_id, None)
            if not job:
                raise KeyError(job_id)
            self._save_jobs()
            self._sync_cron_jobs()
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
        next_run_time = getattr(scheduled_job, "next_run_time", None) if scheduled_job else None
        if next_run_time:
            next_run = next_run_time.isoformat()
        return {
            "auth_mode": "browser" if self.use_browser_session else "credentials",
            "next_run_at": next_run,
            "jobs": [job.to_dict() for job in self.list_jobs()],
            "history": list(self.history)[:30],
        }

    def list_venues(self, search: str = "") -> list[dict[str, Any]]:
        client = self.create_client()
        venues, _ = client.list_all_venues(venue_name=search)
        return [venue.raw for venue in venues]

    def get_venue_detail(self, venue_id: str) -> dict[str, Any]:
        client = self.create_client()
        detail = client.get_venue_detail(venue_id)
        return {
            "venue_id": detail.venue_id,
            "venue_name": detail.venue_name,
            "campus_name": detail.campus_name,
            "open_time": detail.open_time,
            "venue_mobile": detail.venue_mobile,
            "motion_types": [asdict(item) for item in detail.motion_types],
        }

    def get_availability(self, venue_id: str, motion: str) -> list[dict[str, Any]]:
        client = self.create_client()
        motion_type = client.resolve_motion_type(venue_id, motion)
        options = client.list_date_options(venue_id, motion_type.id)
        results = []
        for option in options:
            slots = client.list_available_slots(
                venue_id,
                motion_type.id,
                date=option.date,
                date_id=option.date_id,
            )
            results.append(
                {
                    "date": option.date,
                    "view_str": option.view_str,
                    "week": option.week,
                    "selectable_count": len(slots),
                    "selectable_slots": [
                        {
                            "field_name": slot.field_name,
                            "time_slot": slot.time_slot,
                            "price": slot.price,
                            "field_id": slot.field_id,
                        }
                        for slot in slots
                    ],
                }
            )
        return results

    def _choose_slots(
        self,
        *,
        slots: list[FieldSlot],
        time_slots: list[str],
        preferred_fields: list[str],
    ) -> tuple[list[FieldSlot], list[str]]:
        selected: list[FieldSlot] = []
        missing: list[str] = []
        preferred_order = {field: index for index, field in enumerate(preferred_fields)}
        for time_slot in time_slots:
            candidates = [slot for slot in slots if slot.time_slot == time_slot]
            if preferred_fields:
                candidates = [slot for slot in candidates if slot.field_name in preferred_fields]
            candidates.sort(
                key=lambda slot: (
                    preferred_order.get(slot.field_name, len(preferred_order)),
                    slot.field_name,
                )
            )
            if not candidates:
                missing.append(time_slot)
                continue
            selected.append(candidates[0])
        return selected, missing

    def build_job_preview(self, client: SportsReservationClient, job: ReservationJob) -> dict[str, Any]:
        if job.job_type == JOB_TYPE_CRON:
            return self._build_cron_preview(client, job)

        motion_type = client.resolve_motion_type(job.venue_id, job.motion)
        date_options = client.list_date_options(job.venue_id, motion_type.id)
        date_map = {item.date: item for item in date_options}
        if job.target_date not in date_map:
            raise SportsAPIError(
                f"{job.target_date} is not in the current reservation window yet."
            )
        date_option: DateOption = date_map[job.target_date]
        live_slots = client.list_available_slots(
            job.venue_id,
            motion_type.id,
            date=job.target_date,
            date_id=date_option.date_id,
        )
        selected_slots, missing = self._choose_slots(
            slots=live_slots,
            time_slots=job.time_slots,
            preferred_fields=job.preferred_fields,
        )
        if missing:
            wanted = ", ".join(missing)
            raise SportsAPIError(f"No selectable slots are available for: {wanted}")
        selected_spaces = client.build_selected_spaces(selected_slots)
        payload = client.build_confirm_order_payload(
            venue_id=job.venue_id,
            motion_type=motion_type,
            date_option=date_option,
            selected_spaces=selected_spaces,
        )
        total_price = sum(float(space["venuePrice"]) for space in selected_spaces)
        return {
            "motion_type": asdict(motion_type),
            "date_option": asdict(date_option),
            "selected_slots": [asdict(slot) for slot in selected_slots],
            "confirm_order_payload": payload,
            "total_price": f"{total_price:.2f}",
        }

    @staticmethod
    def _slot_start_at(date_str: str, time_slot: str) -> datetime:
        start_text = time_slot.split("-", 1)[0]
        return datetime.strptime(f"{date_str} {start_text}", "%Y-%m-%d %H:%M")

    def _build_cron_preview(self, client: SportsReservationClient, job: ReservationJob) -> dict[str, Any]:
        motion_type = client.resolve_motion_type(job.venue_id, job.motion)
        date_options = client.list_date_options(job.venue_id, motion_type.id)
        today = _now().date()
        window_start = today + timedelta(days=job.window_start_days)
        window_end = today + timedelta(days=job.window_end_days)
        wanted_times = set(job.time_slots)
        now = _now()
        saw_date_in_window = False
        cutoff_filtered = False

        for date_option in date_options:
            date_value = _parse_date(date_option.date)
            if date_value < window_start or date_value > window_end:
                continue
            saw_date_in_window = True
            live_slots = client.list_available_slots(
                job.venue_id,
                motion_type.id,
                date=date_option.date,
                date_id=date_option.date_id,
            )
            filtered_slots: list[FieldSlot] = []
            for slot in live_slots:
                if slot.time_slot not in wanted_times:
                    continue
                slot_start = self._slot_start_at(date_option.date, slot.time_slot)
                if slot_start - now < timedelta(hours=job.redeem_deadline_hours):
                    cutoff_filtered = True
                    continue
                filtered_slots.append(slot)

            selected_slots, missing = self._choose_slots(
                slots=filtered_slots,
                time_slots=job.time_slots,
                preferred_fields=job.preferred_fields,
            )
            if missing:
                continue

            selected_spaces = client.build_selected_spaces(selected_slots)
            payload = client.build_confirm_order_payload(
                venue_id=job.venue_id,
                motion_type=motion_type,
                date_option=date_option,
                selected_spaces=selected_spaces,
            )
            total_price = sum(float(space["venuePrice"]) for space in selected_spaces)
            return {
                "motion_type": asdict(motion_type),
                "date_option": asdict(date_option),
                "selected_slots": [asdict(slot) for slot in selected_slots],
                "confirm_order_payload": payload,
                "total_price": f"{total_price:.2f}",
            }

        if not saw_date_in_window:
            raise SportsAPIError(
                f"No reservable dates are currently exposed between {window_start.isoformat()} and {window_end.isoformat()}."
            )
        if cutoff_filtered:
            raise SportsAPIError(
                "Matching slots exist but are already past the redeem deadline."
            )
        raise SportsAPIError(
            "No selectable slots are available in the configured cron window for the requested time slots."
        )

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
            self._save_jobs()
            self._sync_cron_jobs()

    def run_job(
        self,
        job_id: str,
        *,
        dry_run: bool = False,
        triggered_by: str = "manual",
    ) -> dict[str, Any]:
        with self.run_lock:
            job = self.get_job(job_id)
            today = _now().date()
            if job.job_type == JOB_TYPE_TARGET_DATE:
                target_date = _parse_date(job.target_date)
                if target_date < today:
                    self._mark_job(job, status="expired", message="Target date has already passed.")
                    self.record_history(job, triggered_by, False, "Target date has already passed.")
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
            if not dry_run and not job.enabled and triggered_by in {"schedule", JOB_TYPE_CRON}:
                return {"ok": False, "message": "Job is disabled."}

            client = self.create_client()
            retry_window_seconds = job.retry_window_seconds if job.job_type == JOB_TYPE_TARGET_DATE else 0
            deadline = time.time() + (retry_window_seconds if not dry_run else 0)
            attempt_count = 0
            while True:
                attempt_count += 1
                try:
                    preview = self.build_job_preview(client, job)
                except SportsAPIError as exc:
                    message = str(exc)
                    should_retry = (
                        not dry_run
                        and triggered_by in {"schedule", "manual"}
                        and time.time() + job.retry_interval_seconds <= deadline
                    )
                    status = "waiting_window" if "window" in message else "no_slots"
                    self._mark_job(job, status=status, message=message)
                    if should_retry:
                        time.sleep(job.retry_interval_seconds)
                        continue
                    self.record_history(job, triggered_by, False, message, details={"attempts": attempt_count})
                    return {"ok": False, "message": message, "attempts": attempt_count}

                if dry_run:
                    message = "Preview built successfully."
                    self._mark_job(job, status="preview_ready", message=message)
                    self.record_history(job, triggered_by, True, message, details={"attempts": attempt_count})
                    return {"ok": True, "message": message, "preview": preview, "attempts": attempt_count}

                response = client.confirm_personal_order(preview["confirm_order_payload"])
                if response.get("code") == 0:
                    order_id = str(response["data"])
                    payment = client.create_payment(order_id)
                    message = f"Reservation order created successfully: {order_id}"
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
                        details={"attempts": attempt_count, "order_id": order_id},
                    )
                    notification = self._send_booking_notification(
                        job,
                        order_id=order_id,
                        payment=payment,
                        preview=preview,
                    )
                    return {
                        "ok": True,
                        "message": message,
                        "order_id": order_id,
                        "payment": payment,
                        "notification": notification,
                        "preview": preview,
                        "attempts": attempt_count,
                    }

                message = response.get("msg", "Unknown reservation error")
                if response.get("code") == 1002:
                    self._mark_job(job, status="captcha_required", message=message)
                    self.record_history(job, triggered_by, False, message, details=response)
                    return {"ok": False, "message": message, "response": response}

                should_retry = time.time() + job.retry_interval_seconds <= deadline
                self._mark_job(job, status="submit_failed", message=message)
                if should_retry:
                    time.sleep(job.retry_interval_seconds)
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
            result = self.notifier.send(
                "\n".join(message_lines),
                title=f"SJTU sports booked: {job.venue_name}",
                tags=["sports", "reservation"],
                click=payment_url,
            )
        except Exception as exc:  # pragma: no cover - network failure path
            logging.getLogger("sports_server").warning("Failed to send ntfy notification: %s", exc)
            self.record_history(job, "notification", False, f"ntfy notification failed: {exc}")
            return {"configured": True, "sent": False, "error": str(exc)}

        details = {"topic": result.topic, "status_code": result.status_code}
        if result.message_id:
            details["message_id"] = result.message_id
        self.record_history(job, "notification", True, "ntfy notification sent.", details=details)
        return {
            "configured": True,
            "sent": True,
            "topic": result.topic,
            "status_code": result.status_code,
            "message_id": result.message_id,
        }

    def run_scheduled_jobs(self) -> None:
        for job in self.list_jobs():
            if not job.enabled or job.job_type != JOB_TYPE_TARGET_DATE:
                continue
            try:
                self.run_job(job.job_id, dry_run=False, triggered_by="schedule")
            except Exception as exc:  # pragma: no cover - defensive logging
                logging.getLogger("sports_server").exception("Scheduled sports job failed: %s", exc)


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
                <input id="preferredFields" placeholder="可选，英文逗号分隔">
            </div>
            <div class="field-row">
                <label for="retryInterval">重试间隔 (秒)</label>
                <input id="retryInterval" type="number" min="1" value="5">
            </div>
        </div>
        <div class="field-row">
            <label>时间段</label>
            <div class="slot-grid" id="timeSlotGrid"></div>
        </div>
        <div class="btn-row">
            <button type="button" class="btn-primary" onclick="createJob()">保存任务</button>
            <button type="button" class="btn-ghost" onclick="refreshStatus()">刷新</button>
        </div>
        <div id="modeHelp" class="info-box">指定日期模式将在中午抢定特定日期的场馆。定时监控模式将每隔几分钟扫描配置的日期窗口内的新释放场地，并在下单截止前预订。</div>
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

function esc(v) {
    return String(v ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function selectedTimeSlots() {
    return [...document.querySelectorAll('input[name="timeSlot"]:checked')].map(i => i.value);
}

function toggleJobMode() {
    const mode = document.getElementById("jobType").value;
    const isCron = mode === "cron";
    document.getElementById("targetDateRow").classList.toggle("hidden", isCron);
    document.getElementById("retryWindowRow").classList.toggle("hidden", isCron);
    document.getElementById("cronWindowRows").classList.toggle("hidden", !isCron);
    document.getElementById("cronTimingRows").classList.toggle("hidden", !isCron);
}

function renderTimeSlots() {
    document.getElementById("timeSlotGrid").innerHTML = TIME_SLOTS.map(s =>
        `<label><input type="checkbox" name="timeSlot" value="${s}"> ${s}</label>`
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
        preferred_fields: document.getElementById("preferredFields").value,
        time_slots: selectedTimeSlots(),
        retry_window_seconds: Number(document.getElementById("retryWindow").value || 180),
        retry_interval_seconds: Number(document.getElementById("retryInterval").value || 5),
        cron_interval_minutes: Number(document.getElementById("cronIntervalMinutes").value || 10),
        window_start_days: Number(document.getElementById("windowStartDays").value || 0),
        window_end_days: Number(document.getElementById("windowEndDays").value || 7),
        redeem_deadline_hours: Number(document.getElementById("redeemDeadlineHours").value || 2),
        enabled: true,
    };
    await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    document.getElementById("jobName").value = "";
    document.getElementById("preferredFields").value = "";
    document.querySelectorAll('input[name="timeSlot"]').forEach(i => { i.checked = false; });
    await refreshStatus();
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
    if (["waiting_window","no_slots","preview_ready"].includes(s)) return "st-waiting_window";
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
                            : `${esc(j.target_date)} · ${esc(j.time_slots.join(", "))}<br>重试窗口: ${esc(j.retry_window_seconds)} 秒`
                        }<br>
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
        return render_template_string(dashboard_html, time_slots=json.dumps(SPORTS_TIME_SLOTS))

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
        app.logger.info(
            "%s %s %s from %s",
            request.method,
            request.path,
            response.status_code,
            get_client_ip(),
        )
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sports reservation daemon.")
    parser.add_argument("-p", "--port", type=int, default=5003, help="Port to listen on (default: 5003)")
    parser.add_argument(
        "--from-browser",
        action="store_true",
        help="Reuse the current Playwright browser session instead of credentials.json.",
    )
    args = parser.parse_args()

    daemon = SportsReservationDaemon(use_browser_session=args.from_browser)
    daemon.start()

    app = create_app(daemon)
    print(f"Starting Sports Reservation Daemon on port {args.port}")
    print(f"Dashboard: http://localhost:{args.port}/")
    print(f"Auth mode: {'browser' if args.from_browser else 'credentials'}")
    app.run(debug=False, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
