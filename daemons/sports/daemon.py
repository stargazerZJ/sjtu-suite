"""Scheduler container for sports reservation jobs."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sjtusuite.clients.sports import (
    CredentialsSportsAuthProvider,
    PreparedTargetDateReservation,
    SportsReservationClient,
)
from sjtusuite.core.credentials import credentials
from sjtusuite.notifications import NtfyNotifier

from .constants import (
    JOBS_FILE,
    LOG_FILE,
    NOON_WARMUP_HOUR,
    NOON_WARMUP_MINUTE,
    NOON_WARMUP_SECOND,
    PREPARED_CONTEXT_TTL_SECONDS,
    TIMEZONE,
)
from .booking import SportsBookingRunnerMixin
from .catalog import SportsCatalogMixin
from .job_management import SportsJobManagementMixin
from .models import PreparedRunContext, ReservationJob
from .utils import (
    LOGGER,
    _format_log_message,
)


class SportsReservationDaemon(
    SportsJobManagementMixin, SportsCatalogMixin, SportsBookingRunnerMixin
):
    def __init__(
        self,
        *,
        jobs_file: Path = JOBS_FILE,
    ):
        self.logger = LOGGER
        self.jobs_file = jobs_file
        self.auth_provider = CredentialsSportsAuthProvider(
            credentials.username,
            credentials.password,
            allow_interactive=False,
        )
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
            auth_mode="credentials",
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
            auth_mode="credentials",
        )
        client = self.auth_provider.create_client()
        self._log(
            logging.DEBUG, "Sports reservation client login completed with credentials."
        )
        return client
