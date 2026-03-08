import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from sjtusuite.auth import JACLogin
from .base import ZoneInfo, ReservationInfo, LibrarySeatClient


class ReservationCache:
    reservations: list[ReservationInfo]
    last_updated: datetime
    _refresh: Callable[[], list[ReservationInfo]]

    def __init__(self, refresh: Callable[[], list[ReservationInfo]]):
        self.reservations = []
        self.last_updated = datetime.min
        self._refresh = refresh

    def update(self, reservations: list[ReservationInfo]):
        self.reservations = reservations
        self.last_updated = datetime.now()

    def get(self, max_age: timedelta = timedelta()) -> list[ReservationInfo]:
        if datetime.now() - self.last_updated > max_age:
            self.refresh()
        return self.reservations

    def refresh(self):
        self.update(self._refresh())


class LibrarySeatAdvancedClient(LibrarySeatClient):
    scheduler: BackgroundScheduler
    reservation_cache: ReservationCache
    _seat_tag_to_id: dict[str, int]
    _seat_id_to_tag: dict[int, str]

    def __init__(self, jac_login: JACLogin, session_file="libseat_client.cookies", name="LibrarySeatAdvancedClient"):
        super().__init__(jac_login, session_file, name=name)
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.reservation_cache = ReservationCache(self.query_reservation)

    def start(self):
        self.scheduler.start()
        self._seat_tag_to_id = self.get_seat_mapping()
        self._seat_id_to_tag = {v: k for k, v in self._seat_tag_to_id.items()}
        self.scheduler.add_job(self.postpone_if_late, id='postpone_if_late', trigger="date",
                               run_date=datetime.now() + timedelta(days=1)).pause()  # Pause the job initially
        self.scheduler.add_job(self.schedule_postpone, id='schedule_postpone', trigger="interval", minutes=15)
        self.logger.warning("Client started.")
        self.schedule_postpone()

    def shutdown(self):
        self.scheduler.shutdown()
        self.logger.warning("Client shutdown.")

    def seat_id_from_tag(self, tag: str) -> int:
        return self._seat_tag_to_id[tag]

    def seat_tag_from_id(self, seat_id: int) -> str:
        return self._seat_id_to_tag[seat_id]

    def get_upcoming_reservation(self, max_age: timedelta = timedelta()) -> ReservationInfo:
        """
        Get the upcoming reservation of the user. Return None if no reservation is found.
        """
        reservations = self.reservation_cache.get(max_age)
        return min(reservations, key=lambda r: r.start_time, default=None)

    def cancel_reservation(self, reservation: ReservationInfo) -> tuple[bool, str]:
        """
        Cancel the reservation.
        """
        return super().cancel_reservation(reservation.id)

    def postpone_if_late(self):
        """
        :brief: Postpone the upcoming reservation if the user is late.
        :details: If the user has not checked in 15 minutes after the start time, the reservation will be postponed by 30 minutes.
        However, if the new reservation time is less than 1 hour, no reservation will be made as it is not allowed.
        """
        self.logger.debug("Checking for late reservations.")
        upcoming_reservation = self.get_upcoming_reservation()
        if upcoming_reservation is None or upcoming_reservation.has_checked_in:
            return
        # if datetime.now() > upcoming_reservation.start_time + timedelta(minutes=1):
        if datetime.now() > upcoming_reservation.start_time + timedelta(minutes=15):
            new_begin_time = upcoming_reservation.start_time + timedelta(minutes=30)
            self.logger.info(f"Canceling reservation {self.seat_tag_from_id(upcoming_reservation.seat_id)}")
            success, message = self.cancel_reservation(upcoming_reservation)
            if not success:
                self.logger.error(
                    f"Failed to cancel reservation {self.seat_tag_from_id(upcoming_reservation.seat_id)}: {message}")
                return
            if upcoming_reservation.end_time - new_begin_time < timedelta(hours=1):
                self.logger.warning(f"Cannot postpone reservation. End time too close: {upcoming_reservation.end_time}")
                return
            success, _, message = self.reserve_seat(upcoming_reservation.seat_id, new_begin_time,
                                                    upcoming_reservation.end_time)
            if not success:
                self.logger.error(
                    f"Failed to postpone reservation {self.seat_tag_from_id(upcoming_reservation.seat_id)}: {message}")
                return
            self.logger.info(
                f"Postponed reservation {self.seat_tag_from_id(upcoming_reservation.seat_id)} to {new_begin_time}")
            self.scheduler.add_job(self.postpone_if_late, id='postpone_if_late', trigger="date",
                                   run_date=upcoming_reservation.end_time, replace_existing=True)

    def schedule_postpone(self):
        """
        Schedule the postpone_if_late method to run 3 minutes before no-show penalty.
        (i.e. 27 minutes after the start time)
        """
        reservations = self.reservation_cache.get(timedelta(minutes=5))
        reservations = list(filter(lambda r: not r.has_checked_in, reservations))
        if not reservations:
            self.logger.debug("No upcoming reservation found. Cancelling postpone job.")
            postpone_job = self.scheduler.get_job('postpone_if_late')
            if postpone_job:
                postpone_job.remove()
            return
        upcoming_reservation = min(reservations, key=lambda r: r.start_time)
        postpone_date = max(
            upcoming_reservation.start_time + timedelta(minutes=27),
            datetime.now() + timedelta(seconds=5)
        )
        # postpone_date = datetime.now() + timedelta(seconds=5)
        if datetime.now() - upcoming_reservation.start_time > timedelta(
                minutes=30) and not upcoming_reservation.has_checked_in:
            self.logger.warning(f"Can't deal with temporary leave for now. Rescheduling postpone job as interval task.")
            self.scheduler.add_job(self.postpone_if_late, id='postpone_if_late', trigger="interval", minutes=10,
                                   replace_existing=True)
            return
        self.scheduler.add_job(self.postpone_if_late, id='postpone_if_late', trigger="date",
                               run_date=postpone_date, replace_existing=True)
        self.logger.info(
            f"Postpone job of reservation {self.seat_tag_from_id(upcoming_reservation.seat_id)} scheduled for {postpone_date}")


if __name__ == "__main__":

    from sjtusuite.auth import get_test_jac_login

    jac_login = get_test_jac_login()

    client = LibrarySeatAdvancedClient(jac_login)
    client.logger.setLevel("DEBUG")
    client.start()

    try:
        while True:
            # Keep the script running
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        client.shutdown()
