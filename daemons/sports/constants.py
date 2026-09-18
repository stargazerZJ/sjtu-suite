"""Shared constants for the sports reservation daemon."""

from sjtusuite.core.config import get_data_dir

TIMEZONE = "Asia/Shanghai"
JOBS_FILE = get_data_dir() / "sports_reservation_jobs.json"
LOG_FILE = get_data_dir() / "sports_reservation_daemon.log"
JOB_TYPE_TARGET_DATE = "target_date"
JOB_TYPE_CRON = "cron"
NOON_WARMUP_HOUR = 11
NOON_WARMUP_MINUTE = 59
NOON_WARMUP_SECOND = 0
PREPARED_CONTEXT_TTL_SECONDS = 240
TARGET_DATE_FAST_RETRY_INTERVAL_SECONDS = 0.2
TARGET_DATE_BURST_WINDOW_SECONDS = 15.0
TARGET_DATE_PREVIEW_BURST_CONCURRENCY = 4
