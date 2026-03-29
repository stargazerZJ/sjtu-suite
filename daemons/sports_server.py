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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
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
from sjtusuite.servers.base import get_client_ip


TIMEZONE = "Asia/Shanghai"
JOBS_FILE = get_data_dir() / "sports_reservation_jobs.json"
LOG_FILE = get_data_dir() / "sports_reservation_daemon.log"


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
    target_date: str
    time_slots: list[str]
    preferred_fields: list[str] = field(default_factory=list)
    enabled: bool = True
    retry_window_seconds: int = 180
    retry_interval_seconds: int = 5
    auto_disable_on_success: bool = True
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
            target_date=payload["target_date"],
            time_slots=_normalize_time_slots(payload.get("time_slots", [])),
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=int(payload.get("retry_window_seconds", 180)),
            retry_interval_seconds=int(payload.get("retry_interval_seconds", 5)),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
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
                key=lambda job: (job.target_date, job.name, job.created_at),
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

        target_date = payload.get("target_date", "")
        _parse_date(target_date)

        job = ReservationJob(
            job_id=str(uuid.uuid4()),
            name=(payload.get("name") or "").strip() or f"{payload['motion']} {target_date}",
            venue_id=payload["venue_id"].strip(),
            venue_name=(payload.get("venue_name") or payload["venue_id"]).strip(),
            motion=payload["motion"].strip(),
            target_date=target_date,
            time_slots=time_slots,
            preferred_fields=_normalize_fields(payload.get("preferred_fields")),
            enabled=bool(payload.get("enabled", True)),
            retry_window_seconds=max(10, int(payload.get("retry_window_seconds", 180))),
            retry_interval_seconds=max(1, int(payload.get("retry_interval_seconds", 5))),
            auto_disable_on_success=bool(payload.get("auto_disable_on_success", True)),
        )
        with self.jobs_lock:
            self.jobs[job.job_id] = job
            self._save_jobs()
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
            if "target_date" in payload:
                _parse_date(payload["target_date"])
                job.target_date = payload["target_date"]
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
            job.updated_at = _now_iso()
            self._save_jobs()
            return job

    def delete_job(self, job_id: str) -> None:
        with self.jobs_lock:
            job = self.jobs.pop(job_id, None)
            if not job:
                raise KeyError(job_id)
            self._save_jobs()
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
            target_date = _parse_date(job.target_date)
            if target_date < today:
                self._mark_job(job, status="expired", message="Target date has already passed.")
                self.record_history(job, triggered_by, False, "Target date has already passed.")
                return {"ok": False, "message": "Target date has already passed."}
            if triggered_by == "schedule" and target_date > today + timedelta(days=7):
                message = "Still outside the current booking window."
                self._mark_job(job, status="waiting_window", message=message)
                self.record_history(job, triggered_by, True, message)
                return {"ok": True, "message": message}
            if not dry_run and not job.enabled and triggered_by == "schedule":
                return {"ok": False, "message": "Job is disabled."}

            client = self.create_client()
            deadline = time.time() + (job.retry_window_seconds if not dry_run else 0)
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
                    return {
                        "ok": True,
                        "message": message,
                        "order_id": order_id,
                        "payment": payment,
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

    def run_scheduled_jobs(self) -> None:
        for job in self.list_jobs():
            if not job.enabled:
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
  <title>Sports Reservation Daemon</title>
  <style>
    :root {
      --bg: #f3efe4;
      --paper: rgba(255, 252, 245, 0.88);
      --ink: #1f1f1f;
      --muted: #6a6257;
      --line: rgba(40, 31, 18, 0.12);
      --accent: #b33a21;
      --accent-2: #d97b29;
      --good: #1f7a4c;
      --warn: #9a5d00;
      --bad: #a22c29;
      --shadow: 0 24px 60px rgba(65, 44, 18, 0.12);
      --font: "SF Pro Display", "Helvetica Neue", Helvetica, Arial, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: var(--font);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(217, 123, 41, 0.28), transparent 28%),
        radial-gradient(circle at top right, rgba(179, 58, 33, 0.18), transparent 24%),
        linear-gradient(180deg, #fbf6eb 0%, #efe5d3 100%);
    }
    .shell {
      max-width: 1200px;
      margin: 0 auto;
      padding: 28px 18px 42px;
    }
    .hero {
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--paper);
      backdrop-filter: blur(10px);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 20px;
    }
    .title {
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.04em;
      margin: 0 0 8px;
    }
    .subtitle {
      margin: 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.6;
    }
    .pill-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 16px;
    }
    .pill {
      border-radius: 999px;
      padding: 10px 14px;
      background: rgba(179, 58, 33, 0.08);
      color: var(--accent);
      font-size: 13px;
      font-weight: 600;
    }
    .grid {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
    }
    .section-title {
      margin: 0 0 14px;
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 6px;
    }
    input, select, button, textarea {
      font: inherit;
    }
    input, select {
      width: 100%;
      border: 1px solid rgba(62, 45, 21, 0.16);
      border-radius: 14px;
      padding: 11px 12px;
      background: white;
    }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .full {
      grid-column: 1 / -1;
    }
    .slot-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-top: 8px;
    }
    .slot-grid label {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      margin: 0;
      border-radius: 12px;
      background: rgba(217, 123, 41, 0.08);
      font-weight: 500;
      cursor: pointer;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    button {
      border: 0;
      border-radius: 999px;
      padding: 11px 16px;
      background: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary {
      background: white;
      color: var(--ink);
      border: 1px solid rgba(62, 45, 21, 0.16);
    }
    button.warn { background: var(--accent-2); }
    button.good { background: var(--good); }
    button.bad { background: var(--bad); }
    .jobs {
      display: grid;
      gap: 12px;
      margin-top: 14px;
    }
    .job {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.8);
    }
    .job-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: start;
    }
    .job-name {
      margin: 0;
      font-size: 20px;
      font-weight: 700;
    }
    .meta {
      margin-top: 8px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: rgba(31, 122, 76, 0.1);
      color: var(--good);
    }
    .status.waiting_window, .status.no_slots, .status.preview_ready { background: rgba(154, 93, 0, 0.12); color: var(--warn); }
    .status.submit_failed, .status.expired, .status.captcha_required { background: rgba(162, 44, 41, 0.12); color: var(--bad); }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .history {
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }
    .history-item {
      border-left: 4px solid rgba(31, 122, 76, 0.5);
      padding: 10px 12px;
      background: rgba(255, 255, 255, 0.74);
      border-radius: 0 14px 14px 0;
    }
    .history-item.bad { border-left-color: rgba(162, 44, 41, 0.55); }
    .muted { color: var(--muted); font-size: 13px; }
    .info-box {
      border-radius: 16px;
      padding: 14px;
      background: rgba(217, 123, 41, 0.08);
      color: var(--muted);
      line-height: 1.6;
    }
    .availability-list {
      display: grid;
      gap: 12px;
      margin-top: 12px;
    }
    .availability-day {
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 12px;
      background: rgba(255, 255, 255, 0.72);
    }
    .availability-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 8px;
    }
    .availability-title {
      font-weight: 700;
      color: var(--ink);
    }
    .slot-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 8px;
    }
    .slot-pill {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      border-radius: 999px;
      padding: 7px 10px;
      background: rgba(31, 122, 76, 0.1);
      color: var(--good);
      font-size: 13px;
      font-weight: 600;
    }
    @media (max-width: 960px) {
      .hero, .grid { grid-template-columns: 1fr; }
      .form-grid, .slot-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="hero">
      <section class="card">
        <p class="section-title">Sports Reservation Daemon</p>
        <h1 class="title">Noon Rush, Handled</h1>
        <p class="subtitle">Configure venue jobs here, then let the daemon wake up at 12:00 every day, check the newly opened reservation window, and place the order automatically when the slot becomes available.</p>
        <div class="pill-row">
          <div class="pill" id="authMode">Auth mode: loading...</div>
          <div class="pill" id="nextRun">Next noon run: loading...</div>
        </div>
      </section>
      <section class="card">
        <p class="section-title">How It Works</p>
        <div class="info-box">
          1. Search and pick a venue.
          <br>
          2. Choose the motion type, date, and target time slots.
          <br>
          3. Optionally list preferred fields like 场地2,场地3.
          <br>
          4. The daemon retries during the noon opening window until it books successfully or times out.
        </div>
      </section>
    </div>

    <div class="grid">
      <section class="card">
        <p class="section-title">Create Job</p>
        <div class="form-grid">
          <div class="full">
            <label for="jobName">Job name</label>
            <input id="jobName" placeholder="e.g. Monday noon ping pong">
          </div>
          <div class="full">
            <label for="venueSearch">Venue search</label>
            <div class="toolbar">
              <input id="venueSearch" placeholder="Search venue name">
              <button type="button" class="secondary" onclick="searchVenues()">Search</button>
            </div>
          </div>
          <div class="full">
            <label for="venueSelect">Venue</label>
            <select id="venueSelect" onchange="loadVenueDetail()">
              <option value="">Choose a venue</option>
            </select>
          </div>
          <div>
            <label for="motionSelect">Motion type</label>
            <select id="motionSelect" onchange="loadAvailability()">
              <option value="">Choose a motion type</option>
            </select>
          </div>
          <div>
            <label for="targetDate">Reservation date</label>
            <input id="targetDate" type="date">
          </div>
          <div>
            <label for="preferredFields">Preferred fields</label>
            <input id="preferredFields" placeholder="Optional, comma separated">
          </div>
          <div>
            <label for="retryWindow">Retry window (seconds)</label>
            <input id="retryWindow" type="number" min="10" value="180">
          </div>
          <div>
            <label for="retryInterval">Retry interval (seconds)</label>
            <input id="retryInterval" type="number" min="1" value="5">
          </div>
          <div class="full">
            <label>Time slots</label>
            <div class="slot-grid" id="timeSlotGrid"></div>
          </div>
        </div>
        <div class="toolbar" style="margin-top:16px;">
          <button type="button" onclick="createJob()">Save job</button>
          <button type="button" class="secondary" onclick="refreshStatus()">Refresh</button>
        </div>
        <div id="availabilityBox" class="info-box" style="margin-top:16px;">Search a venue to load live motion types and visible dates.</div>
      </section>

      <section class="card">
        <p class="section-title">Recent Attempts</p>
        <div id="history" class="history"></div>
      </section>
    </div>

    <section class="card" style="margin-top:18px;">
      <p class="section-title">Jobs</p>
      <div id="jobs" class="jobs"></div>
    </section>
  </div>

  <script>
    const TIME_SLOTS = {{ time_slots | safe }};
    let cachedVenues = [];

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function selectedTimeSlots() {
      return [...document.querySelectorAll('input[name="timeSlot"]:checked')].map((item) => item.value);
    }

    function renderTimeSlots() {
      const root = document.getElementById("timeSlotGrid");
      root.innerHTML = TIME_SLOTS.map((slot) => `
        <label><input type="checkbox" name="timeSlot" value="${slot}"> ${slot}</label>
      `).join("");
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || payload.message || "Request failed");
      }
      return payload;
    }

    async function searchVenues() {
      const search = document.getElementById("venueSearch").value.trim();
      const payload = await api(`/api/catalog/venues?search=${encodeURIComponent(search)}`);
      cachedVenues = payload.venues || [];
      const select = document.getElementById("venueSelect");
      select.innerHTML = `<option value="">Choose a venue</option>` + cachedVenues.map((venue) => `
        <option value="${venue.venueId}">${escapeHtml(venue.venueName)} · ${escapeHtml(venue.campusName || "")}</option>
      `).join("");
    }

    async function loadVenueDetail() {
      const venueId = document.getElementById("venueSelect").value;
      const motionSelect = document.getElementById("motionSelect");
      motionSelect.innerHTML = `<option value="">Choose a motion type</option>`;
      if (!venueId) {
        return;
      }
      const payload = await api(`/api/catalog/venues/${venueId}`);
      const venue = payload.venue;
      motionSelect.innerHTML += venue.motion_types.map((motion) => `
        <option value="${motion.name}">${escapeHtml(motion.name)}</option>
      `).join("");
      document.getElementById("availabilityBox").innerHTML = `
        <strong>${escapeHtml(venue.venue_name)}</strong><br>
        ${escapeHtml(venue.campus_name)} · ${escapeHtml(venue.open_time)}<br>
        ${escapeHtml(venue.venue_mobile)}
      `;
    }

    async function loadAvailability() {
      const venueId = document.getElementById("venueSelect").value;
      const motion = document.getElementById("motionSelect").value;
      if (!venueId || !motion) {
        return;
      }
      const payload = await api(`/api/catalog/venues/${venueId}/availability?motion=${encodeURIComponent(motion)}`);
      const rows = payload.availability.map((item) => {
        const slots = item.selectable_slots || [];
        const slotMarkup = slots.length
          ? `<div class="slot-pills">${slots.map((slot) => `
              <span class="slot-pill">${escapeHtml(slot.field_name)} · ${escapeHtml(slot.time_slot)} · ¥${escapeHtml(slot.price)}</span>
            `).join("")}</div>`
          : `<div class="muted">No selectable slots right now.</div>`;
        return `
          <div class="availability-day">
            <div class="availability-head">
              <div class="availability-title">${escapeHtml(item.date)} · ${escapeHtml(item.view_str)}</div>
              <div class="muted">${item.selectable_count} selectable</div>
            </div>
            ${slotMarkup}
          </div>
        `;
      });
      document.getElementById("availabilityBox").innerHTML = `
        <strong>Visible reservation window</strong><br>
        <div class="availability-list">${rows.join("") || "No visible dates right now."}</div>
      `;
    }

    async function createJob() {
      const venueSelect = document.getElementById("venueSelect");
      const venueId = venueSelect.value;
      const venueName = venueSelect.selectedOptions[0]?.textContent?.split(" · ")[0] || "";
      const payload = {
        name: document.getElementById("jobName").value.trim(),
        venue_id: venueId,
        venue_name: venueName,
        motion: document.getElementById("motionSelect").value,
        target_date: document.getElementById("targetDate").value,
        preferred_fields: document.getElementById("preferredFields").value,
        time_slots: selectedTimeSlots(),
        retry_window_seconds: Number(document.getElementById("retryWindow").value || 180),
        retry_interval_seconds: Number(document.getElementById("retryInterval").value || 5),
        enabled: true,
      };
      await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      document.getElementById("jobName").value = "";
      document.getElementById("preferredFields").value = "";
      document.querySelectorAll('input[name="timeSlot"]').forEach((item) => { item.checked = false; });
      await refreshStatus();
    }

    async function toggleJob(jobId, enabled) {
      await api(`/api/jobs/${jobId}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled }),
      });
      await refreshStatus();
    }

    async function deleteJob(jobId) {
      await api(`/api/jobs/${jobId}`, { method: "DELETE" });
      await refreshStatus();
    }

    async function runJob(jobId, dryRun) {
      const payload = await api(`/api/jobs/${jobId}/run`, {
        method: "POST",
        body: JSON.stringify({ dry_run: dryRun }),
      });
      const message = payload.result?.message || payload.result?.order_id || "Run finished.";
      alert(message);
      await refreshStatus();
    }

    function renderJobs(jobs) {
      const root = document.getElementById("jobs");
      if (!jobs.length) {
        root.innerHTML = `<div class="muted">No jobs yet. Create one from the panel above.</div>`;
        return;
      }
      root.innerHTML = jobs.map((job) => `
        <article class="job">
          <div class="job-head">
            <div>
              <h2 class="job-name">${escapeHtml(job.name)}</h2>
              <div class="meta">
                ${escapeHtml(job.venue_name)} · ${escapeHtml(job.motion)}<br>
                ${escapeHtml(job.target_date)} · ${escapeHtml(job.time_slots.join(", "))}<br>
                Preferred fields: ${escapeHtml(job.preferred_fields.join(", ") || "Any")}
              </div>
            </div>
            <div class="status ${escapeHtml(job.last_status)}">${escapeHtml(job.last_status)}</div>
          </div>
          <div class="meta" style="margin-top:12px;">
            ${escapeHtml(job.last_message || "No attempts yet.")}<br>
            Last checked: ${escapeHtml(job.last_checked_at || "Never")}<br>
            Order ID: ${escapeHtml(job.last_order_id || "None")}
          </div>
          <div class="actions">
            <button class="secondary" onclick="runJob('${job.job_id}', true)">Preview</button>
            <button class="good" onclick="runJob('${job.job_id}', false)">Run now</button>
            <button class="warn" onclick="toggleJob('${job.job_id}', ${job.enabled ? "false" : "true"})">${job.enabled ? "Disable" : "Enable"}</button>
            <button class="bad" onclick="deleteJob('${job.job_id}')">Delete</button>
          </div>
        </article>
      `).join("");
    }

    function renderHistory(items) {
      const root = document.getElementById("history");
      if (!items.length) {
        root.innerHTML = `<div class="muted">No daemon activity yet.</div>`;
        return;
      }
      root.innerHTML = items.map((item) => `
        <div class="history-item ${item.success ? "" : "bad"}">
          <strong>${escapeHtml(item.job_name)}</strong><br>
          ${escapeHtml(item.message)}<br>
          <span class="muted">${escapeHtml(item.action)} · ${escapeHtml(item.timestamp)}</span>
        </div>
      `).join("");
    }

    async function refreshStatus() {
      const payload = await api("/api/status");
      document.getElementById("authMode").textContent = `Auth mode: ${payload.auth_mode}`;
      document.getElementById("nextRun").textContent = `Next noon run: ${payload.next_run_at || "not scheduled"}`;
      renderJobs(payload.jobs || []);
      renderHistory(payload.history || []);
    }

    renderTimeSlots();
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
