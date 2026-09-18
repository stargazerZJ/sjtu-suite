"""Job persistence and management for the sports reservation daemon."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.interval import IntervalTrigger
from sjtusuite.clients.sports import (
    SLOT_SELECTION_MODE_ALL_REQUIRED,
    SLOT_SELECTION_MODE_FIRST_AVAILABLE,
)

from .constants import (
    JOB_TYPE_CRON,
    JOB_TYPE_TARGET_DATE,
    TIMEZONE,
)
from .models import ReservationJob
from .utils import (
    _cron_waiting_message,
    _default_retry_interval_seconds,
    _next_noon_run_date_iso,
    _normalize_fields,
    _normalize_time_slots,
    _now,
    _now_iso,
    _parse_date,
    _run_date_message,
)


class SportsJobManagementMixin:
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
            "auth_mode": "credentials",
            "next_run_at": next_run,
            "jobs": [job.to_dict() for job in self.list_jobs()],
            "history": list(self.history)[:30],
        }
