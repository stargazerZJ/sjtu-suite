"""Booking preview, submission, retry, and notification logic."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sjtusuite.clients.sports import (
    PreparedTargetDateReservation,
    SportsAPIError,
    SportsReservationClient,
)

from .constants import (
    JOB_TYPE_CRON,
    JOB_TYPE_TARGET_DATE,
    TARGET_DATE_BURST_WINDOW_SECONDS,
    TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS,
    TARGET_DATE_PREVIEW_BURST_CONCURRENCY,
)
from .models import ReservationJob
from .utils import (
    _clone_prepared_target_date_reservation,
    _format_log_message,
    _is_transport_error,
    _now,
    _now_iso,
    _parse_date,
)


class SportsBookingRunnerMixin:
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
