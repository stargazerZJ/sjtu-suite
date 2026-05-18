from __future__ import annotations

import base64
import secrets
import string
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import quote, urlencode

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad
from requests.cookies import create_cookie

from sjtusuite.auth import InteractiveAuthenticationRequired, JACLogin, OAuthClientBase


SPORTS_CLIENT_ID = "mB5nKHqC00MusWAgnqSF"
SPORTS_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArKZOdKQAL+iYzJ4Q5EQzwv/yvVPnfdNVKRgNG19HbCYM4qIzFPEOFv28SVFQh+xqAj8tAfjpMSTihFwt6BQuWfZXWYpAqf4jF4cU7ez/VHJyzsn8Cb7Lf/1KsLpuz+MbqufrA57AysnLAnRXHOwik+QnpsXZYjTcjgxQ0iLMe5iJyo06CKFxH1rmgYMwS4E89kNg1VtYrFKs1MajApfhu9hTEXnm/lP24TPdefRXbf+z84p1GLue2HRhZs3wECH1HJWZOsrdL/M+wigWldY0fHoiaKsjD9rK1NyaPtk4bIYuwPsfQu5RN4hkEPpTvdw1nKzOdo77zNa5ovCY0uNLZwIDAQAB"
)
DEFAULT_RETURN_URL = "https://sports.sjtu.edu.cn/#/paymentResult/1"
SPORTS_TIME_SLOTS = [f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(7, 22)]
UNAVAILABLE_STATUSES = {-3, -2, -1}
SLOT_SELECTION_MODE_ALL_REQUIRED = "all_required"
SLOT_SELECTION_MODE_FIRST_AVAILABLE = "first_available"
SPORTS_REQUEST_TIMEOUT_SECONDS = 5.0


class SportsAPIError(RuntimeError):
    """Raised when the sports reservation API returns an unexpected result."""


@dataclass(slots=True, frozen=True)
class MotionType:
    id: str
    name: str
    tension: int | None
    write_off_type: str | None


@dataclass(slots=True, frozen=True)
class VenueSummary:
    venue_id: str
    venue_name: str
    campus_name: str
    open_time: str
    raw: dict[str, Any]


@dataclass(slots=True, frozen=True)
class VenueDetail:
    venue_id: str
    venue_name: str
    campus_name: str
    open_time: str
    venue_mobile: str
    motion_types: tuple[MotionType, ...]
    raw: dict[str, Any]


@dataclass(slots=True, frozen=True)
class DateOption:
    date: str
    date_id: str
    week: str
    view_str: str
    is_weekend: bool
    raw: dict[str, Any]


@dataclass(slots=True, frozen=True)
class FieldSlot:
    field_id: str
    field_name: str
    time_slot: str
    price: str
    count: int
    status: int
    sign: str | None
    raw: dict[str, Any]

    @property
    def is_selectable(self) -> bool:
        return self.status == 0


@dataclass(slots=True)
class PreparedTargetDateReservation:
    motion_type: MotionType
    date_option: DateOption | None = None


@dataclass(slots=True, frozen=True)
class ReservationAttemptCandidate:
    selected_slots: tuple[FieldSlot, ...]
    selected_spaces: tuple[dict[str, Any], ...]
    confirm_order_payload: dict[str, Any]
    total_price: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_slots": [asdict(slot) for slot in self.selected_slots],
            "selected_spaces": list(self.selected_spaces),
            "confirm_order_payload": self.confirm_order_payload,
            "total_price": self.total_price,
        }


@dataclass(slots=True, frozen=True)
class ReservationPreview:
    motion_type: MotionType
    date_option: DateOption
    selected_slots: tuple[FieldSlot, ...]
    selected_spaces: tuple[dict[str, Any], ...]
    confirm_order_payload: dict[str, Any]
    total_price: str
    fallback_candidates: tuple[ReservationAttemptCandidate, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "motion_type": asdict(self.motion_type),
            "date_option": asdict(self.date_option),
            "selected_slots": [asdict(slot) for slot in self.selected_slots],
            "selected_spaces": list(self.selected_spaces),
            "confirm_order_payload": self.confirm_order_payload,
            "total_price": self.total_price,
            "fallback_candidates": [candidate.to_dict() for candidate in self.fallback_candidates],
        }


def _format_view_str(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = "一二三四五六日"[dt.weekday()] if dt.weekday() < 6 else "日"
    return f"{dt.month:02d}月{dt.day:02d}日 (周{weekday})"


class SportsAuthProvider:
    """Creates authenticated sports clients for a specific runtime context."""

    def create_client(self) -> "SportsReservationClient":
        raise NotImplementedError


class CredentialsSportsAuthProvider(SportsAuthProvider):
    """Authenticate sports clients with JAccount credentials."""

    def __init__(
        self,
        username: str | None,
        password: str | None,
        *,
        allow_interactive: bool = False,
        session_file: str = "sports_client.cookies",
    ):
        self.username = username or ""
        self.password = password or ""
        self.allow_interactive = allow_interactive
        self.session_file = session_file

    def create_client(self) -> "SportsReservationClient":
        if not self.username or not self.password:
            raise SportsAPIError(
                "Sports authentication needs JAccount username/password in credentials.json or environment."
            )
        jac_login = JACLogin(
            self.username,
            self.password,
            allow_interactive=self.allow_interactive,
        )
        client = SportsReservationClient(jac_login, session_file=self.session_file)
        try:
            client.login()
        except InteractiveAuthenticationRequired as exc:
            raise SportsAPIError(str(exc)) from exc
        return client


class SportsReservationClient(OAuthClientBase):
    """Client for the SJTU sports venue reservation system."""

    def __init__(
        self,
        jac_login: JACLogin,
        session_file: str = "sports_client.cookies",
        name: str = "SportsReservationClient",
    ):
        super().__init__(name, session_file)
        self.base_url = "https://sports.sjtu.edu.cn"
        self.redirect_url = f"{self.base_url}/oauth2Login"
        self.jac_login = jac_login
        self.session.headers.update({"Referer": f"{self.base_url}/pc/"})

    def apply_cookies(self, cookies: Iterable[dict[str, Any]]) -> None:
        for cookie in cookies:
            if not cookie.get("name") or not cookie.get("value"):
                continue
            self.session.cookies.set_cookie(
                create_cookie(
                    name=cookie["name"],
                    value=cookie["value"],
                    domain=cookie.get("domain", "sports.sjtu.edu.cn"),
                    path=cookie.get("path", "/"),
                    secure=bool(cookie.get("secure", False)),
                    expires=cookie.get("expires") if isinstance(cookie.get("expires"), int | float) else None,
                )
            )

    def export_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for cookie in self.session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": cookie.secure,
                    "expires": cookie.expires,
                }
            )
        return cookies

    def clone_with_session(self, *, name_suffix: str = "clone") -> "SportsReservationClient":
        jac_login = JACLogin(
            self.jac_login.username,
            self.jac_login.password,
            allow_interactive=self.jac_login.allow_interactive,
        )
        clone = SportsReservationClient(
            jac_login,
            session_file=self.session.cookies.filename or "sports_client.cookies",
            name=f"{self.logger.name}-{name_suffix}",
        )
        clone.session.headers.clear()
        clone.session.headers.update(self.session.headers)
        clone.apply_cookies(self.export_cookies())
        return clone

    def validate_session(self) -> bool:
        response = self._request(
            "GET",
            "/system/user/currentUser",
            retry_auth=False,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False
        if "application/json" not in self._response_header(response, "Content-Type"):
            return False
        payload = response.json()
        data = payload.get("data") or {}
        return payload.get("msg") == "操作成功" and bool(data.get("userId") or data.get("loginName"))

    def login(self, force: bool = False):
        if not force and self.validate_session():
            return
        auth_url = (
            f"{self.jac_base_url}/oauth2/authorize"
            f"?response_type=code&client_id={SPORTS_CLIENT_ID}"
            f"&redirect_uri={quote(self.redirect_url, safe=':/')}"
        )
        try:
            final_redirect_url = self.jac_login.login(auth_url)
            self.session.get(final_redirect_url, allow_redirects=True, timeout=SPORTS_REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise SportsAPIError(f"Sports login request failed: {exc}") from exc
        if not self.validate_session():
            raise SportsAPIError("Sports login failed.")
        self.save_session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        retry_auth: bool = True,
        allow_redirects: bool = False,
        **kwargs,
    ):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request_timeout = kwargs.pop("timeout", SPORTS_REQUEST_TIMEOUT_SECONDS)
        try:
            response = self.session.request(
                method,
                url,
                allow_redirects=allow_redirects,
                timeout=request_timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise SportsAPIError(f"Sports request failed for {method} {url}: {exc}") from exc
        if retry_auth and self._looks_like_login(response):
            self.login(force=True)
            return self._request(
                method,
                path,
                retry_auth=False,
                allow_redirects=allow_redirects,
                **kwargs,
            )
        return response

    def _json_request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        response = self._request(method, path, **kwargs)
        content_type = self._response_header(response, "Content-Type")
        if "application/json" not in content_type:
            raise SportsAPIError(
                f"Expected JSON from {path}, got {content_type or 'unknown'}."
            )
        return response.json()

    @staticmethod
    def _looks_like_login(response) -> bool:
        location = SportsReservationClient._response_header(response, "Location")
        if response.status_code == 302 and location.startswith("/login"):
            return True
        content_type = SportsReservationClient._response_header(response, "Content-Type")
        if "text/html" in content_type:
            return "交通大学场馆预约" in response.text or "/jaccount/ulogin" in response.text
        return False

    @staticmethod
    def _response_header(response, name: str) -> str:
        return response.headers.get(name) or response.headers.get(name.lower()) or ""

    @staticmethod
    def _encode_form(data: dict[str, Any]) -> str:
        pairs: list[tuple[str, str]] = []
        for key, value in data.items():
            if isinstance(value, (list, tuple)):
                if not value:
                    pairs.append((key, ""))
                else:
                    pairs.extend((key, str(item)) for item in value)
            elif value is None:
                pairs.append((key, ""))
            else:
                pairs.append((key, str(value)))
        return urlencode(pairs)

    @staticmethod
    def _aes_encrypt(plaintext: str, key: str) -> str:
        cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
        return base64.b64encode(encrypted).decode("utf-8")

    @staticmethod
    def _rsa_encrypt(plaintext: str) -> str:
        public_key = RSA.import_key(base64.b64decode(SPORTS_PUBLIC_KEY))
        cipher = PKCS1_v1_5.new(public_key)
        encrypted = cipher.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")

    @staticmethod
    def _random_aes_key(length: int = 16) -> str:
        alphabet = string.digits + string.ascii_uppercase
        return "".join(secrets.choice(alphabet) for _ in range(length))

    @staticmethod
    def _build_client_uid(prefix: str = "slider") -> str:
        return f"{prefix}-{uuid.uuid4()}"

    def list_venues(
        self,
        *,
        page_num: int = 1,
        page_size: int = 12,
        campus_ids: Iterable[str] | None = None,
        type_ids: Iterable[str] | None = None,
        venue_name: str = "",
        personal: bool = True,
    ) -> tuple[list[VenueSummary], int]:
        payload = self._json_request(
            "POST",
            "/manage/venue/list",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._encode_form(
                {
                    "pageSize": page_size,
                    "pageNum": page_num,
                    "campusIds": list(campus_ids or []),
                    "typeIds": list(type_ids or []),
                    "venueName": venue_name,
                    "flag": 0 if personal else 1,
                }
            ),
        )
        rows = payload.get("rows", [])
        venues = [
            VenueSummary(
                venue_id=row["venueId"],
                venue_name=row["venueName"],
                campus_name=row.get("campusName", ""),
                open_time=row.get("openTime", ""),
                raw=row,
            )
            for row in rows
        ]
        return venues, int(payload.get("total", len(venues)))

    def list_all_venues(
        self,
        *,
        page_size: int = 12,
        campus_ids: Iterable[str] | None = None,
        type_ids: Iterable[str] | None = None,
        venue_name: str = "",
        personal: bool = True,
    ) -> tuple[list[VenueSummary], int]:
        venues: list[VenueSummary] = []
        seen_ids: set[str] = set()
        reported_total = 0
        page_num = 1
        while True:
            page_rows, page_total = self.list_venues(
                page_num=page_num,
                page_size=page_size,
                campus_ids=campus_ids,
                type_ids=type_ids,
                venue_name=venue_name,
                personal=personal,
            )
            reported_total = max(reported_total, page_total)
            if not page_rows:
                break
            new_rows = 0
            for venue in page_rows:
                if venue.venue_id in seen_ids:
                    continue
                seen_ids.add(venue.venue_id)
                venues.append(venue)
                new_rows += 1
            if len(page_rows) < page_size or new_rows == 0:
                break
            page_num += 1
        return venues, reported_total

    def get_venue_detail(self, venue_id: str) -> VenueDetail:
        payload = self._json_request(
            "POST",
            "/manage/venue/queryVenueById",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._encode_form({"id": venue_id}),
        )
        data = payload.get("data") or {}
        motion_types = tuple(
            MotionType(
                id=item["id"],
                name=item["name"],
                tension=item.get("tension"),
                write_off_type=item.get("writeOffType"),
            )
            for item in data.get("motionTypes", [])
        )
        return VenueDetail(
            venue_id=data["venueId"],
            venue_name=data["venueName"],
            campus_name=data.get("campusName", ""),
            open_time=data.get("openTime", ""),
            venue_mobile=data.get("venueMobile", ""),
            motion_types=motion_types,
            raw=data,
        )

    def resolve_motion_type(self, venue_id: str, motion: str) -> MotionType:
        detail = self.get_venue_detail(venue_id)
        for item in detail.motion_types:
            if item.id == motion or item.name == motion:
                return item
        raise SportsAPIError(f"Motion type {motion!r} not found for venue {detail.venue_name}.")

    def get_venue_notice(self, venue_id: str) -> dict[str, Any]:
        return self._json_request("POST", f"/venue/notice/newStadiumNotice?id={venue_id}")

    def list_availability(self, venue_id: str, motion: str) -> list[dict[str, Any]]:
        motion_type = self.resolve_motion_type(venue_id, motion)
        options = self.list_date_options(venue_id, motion_type.id)
        results: list[dict[str, Any]] = []
        for option in options:
            slots = self.list_available_slots(
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

    def list_date_options(
        self,
        venue_id: str,
        motion_type_id: str,
        *,
        base_date: str | None = None,
    ) -> list[DateOption]:
        base_date = base_date or datetime.now().strftime("%Y-%m-%d")
        payload = self._json_request(
            "POST",
            "/manage/fieldDetail/queryFieldReserveSituationIsFull",
            json={"id": venue_id, "feildType": motion_type_id, "date": base_date},
        )
        options: list[DateOption] = []
        for item in payload.get("data", []):
            dt = datetime.strptime(item["date"], "%Y-%m-%d")
            options.append(
                DateOption(
                    date=item["date"],
                    date_id=item["dateId"],
                    week=item.get("week", str((dt.weekday() + 1) % 7)),
                    view_str=item.get("viewStr", _format_view_str(item["date"])),
                    is_weekend=dt.weekday() >= 5,
                    raw=item,
                )
            )
        return options

    def get_field_situation(
        self,
        venue_id: str,
        motion_type_id: str,
        *,
        date: str,
        date_id: str,
    ) -> tuple[list[FieldSlot], dict[str, Any]]:
        payload = self._json_request(
            "POST",
            "/manage/fieldDetail/queryFieldSituation",
            json={
                "fieldType": motion_type_id,
                "date": date,
                "venueId": venue_id,
                "dateId": date_id,
            },
        )
        slots: list[FieldSlot] = []
        for field in payload.get("data", []):
            for index, slot in enumerate(field.get("priceList", [])):
                slots.append(
                    FieldSlot(
                        field_id=field["fieldId"],
                        field_name=field["fieldName"],
                        time_slot=SPORTS_TIME_SLOTS[index],
                        price=str(slot.get("price", "")),
                        count=int(slot.get("count", 0)),
                        status=int(slot.get("status", -9)),
                        sign=slot.get("sign"),
                        raw=slot,
                    )
                )
        return slots, payload

    def list_available_slots(
        self,
        venue_id: str,
        motion_type_id: str,
        *,
        date: str,
        date_id: str,
    ) -> list[FieldSlot]:
        slots, _ = self.get_field_situation(
            venue_id,
            motion_type_id,
            date=date,
            date_id=date_id,
        )
        return [slot for slot in slots if slot.is_selectable]

    @staticmethod
    def choose_slots(
        *,
        slots: list[FieldSlot],
        time_slots: Iterable[str],
        preferred_fields: Iterable[str] | None = None,
        slot_selection_mode: str = SLOT_SELECTION_MODE_ALL_REQUIRED,
    ) -> tuple[list[FieldSlot], list[str]]:
        requested_time_slots = list(time_slots)
        preferred_field_list = [field for field in (preferred_fields or []) if field]
        preferred_order = {field: index for index, field in enumerate(preferred_field_list)}
        selected: list[FieldSlot] = []
        missing: list[str] = []
        for time_slot in requested_time_slots:
            candidates = [slot for slot in slots if slot.time_slot == time_slot]
            if preferred_field_list:
                candidates = [slot for slot in candidates if slot.field_name in preferred_field_list]
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
            if slot_selection_mode == SLOT_SELECTION_MODE_FIRST_AVAILABLE:
                break
        if slot_selection_mode == SLOT_SELECTION_MODE_FIRST_AVAILABLE and selected:
            return selected, []
        return selected, missing

    @staticmethod
    def rank_slot_candidates(
        *,
        slots: list[FieldSlot],
        time_slots: Iterable[str],
        preferred_fields: Iterable[str] | None = None,
    ) -> tuple[list[FieldSlot], list[str]]:
        requested_time_slots = list(time_slots)
        preferred_field_list = [field for field in (preferred_fields or []) if field]
        preferred_order = {field: index for index, field in enumerate(preferred_field_list)}
        ranked: list[FieldSlot] = []
        missing: list[str] = []
        for time_slot in requested_time_slots:
            candidates = [slot for slot in slots if slot.time_slot == time_slot]
            if preferred_field_list:
                candidates = [slot for slot in candidates if slot.field_name in preferred_field_list]
            candidates.sort(
                key=lambda slot: (
                    preferred_order.get(slot.field_name, len(preferred_order)),
                    slot.field_name,
                )
            )
            if not candidates:
                missing.append(time_slot)
                continue
            ranked.extend(candidates)
        return ranked, missing

    @staticmethod
    def _slot_start_at(date_str: str, time_slot: str) -> datetime:
        start_text = time_slot.split("-", 1)[0]
        return datetime.strptime(f"{date_str} {start_text}", "%Y-%m-%d %H:%M")

    def prepare_target_date_reservation(
        self,
        *,
        venue_id: str,
        motion: str,
    ) -> PreparedTargetDateReservation:
        return PreparedTargetDateReservation(motion_type=self.resolve_motion_type(venue_id, motion))

    def _build_reservation_preview(
        self,
        *,
        venue_id: str,
        motion_type: MotionType,
        date_option: DateOption,
        selected_slots: list[FieldSlot],
        fallback_candidates: tuple[ReservationAttemptCandidate, ...] = (),
    ) -> ReservationPreview:
        selected_spaces = tuple(self.build_selected_spaces(selected_slots))
        payload = self.build_confirm_order_payload(
            venue_id=venue_id,
            motion_type=motion_type,
            date_option=date_option,
            selected_spaces=list(selected_spaces),
        )
        total_price = sum(float(space["venuePrice"]) for space in selected_spaces)
        return ReservationPreview(
            motion_type=motion_type,
            date_option=date_option,
            selected_slots=tuple(selected_slots),
            selected_spaces=selected_spaces,
            confirm_order_payload=payload,
            total_price=f"{total_price:.2f}",
            fallback_candidates=fallback_candidates,
        )

    def _build_attempt_candidate(
        self,
        *,
        venue_id: str,
        motion_type: MotionType,
        date_option: DateOption,
        selected_slots: list[FieldSlot],
    ) -> ReservationAttemptCandidate:
        selected_spaces = tuple(self.build_selected_spaces(selected_slots))
        payload = self.build_confirm_order_payload(
            venue_id=venue_id,
            motion_type=motion_type,
            date_option=date_option,
            selected_spaces=list(selected_spaces),
        )
        total_price = sum(float(space["venuePrice"]) for space in selected_spaces)
        return ReservationAttemptCandidate(
            selected_slots=tuple(selected_slots),
            selected_spaces=selected_spaces,
            confirm_order_payload=payload,
            total_price=f"{total_price:.2f}",
        )

    def build_target_date_preview(
        self,
        *,
        venue_id: str,
        motion: str,
        target_date: str,
        time_slots: Iterable[str],
        preferred_fields: Iterable[str] | None = None,
        slot_selection_mode: str = SLOT_SELECTION_MODE_ALL_REQUIRED,
        prepared: PreparedTargetDateReservation | None = None,
    ) -> ReservationPreview:
        prepared_reservation = prepared or self.prepare_target_date_reservation(
            venue_id=venue_id,
            motion=motion,
        )
        date_option = prepared_reservation.date_option
        if not date_option or date_option.date != target_date:
            date_options = self.list_date_options(venue_id, prepared_reservation.motion_type.id)
            date_map = {item.date: item for item in date_options}
            if target_date not in date_map:
                raise SportsAPIError(f"{target_date} is not in the current reservation window yet.")
            date_option = date_map[target_date]
            prepared_reservation.date_option = date_option
        live_slots = self.list_available_slots(
            venue_id,
            prepared_reservation.motion_type.id,
            date=target_date,
            date_id=date_option.date_id,
        )
        fallback_candidates: tuple[ReservationAttemptCandidate, ...] = ()
        if slot_selection_mode == SLOT_SELECTION_MODE_FIRST_AVAILABLE:
            ranked_slots, missing = self.rank_slot_candidates(
                slots=live_slots,
                time_slots=time_slots,
                preferred_fields=preferred_fields,
            )
            if not ranked_slots:
                wanted = ", ".join(missing)
                raise SportsAPIError(f"No selectable slots are available across preferred fallback times: {wanted}")
            fallback_candidates = tuple(
                self._build_attempt_candidate(
                    venue_id=venue_id,
                    motion_type=prepared_reservation.motion_type,
                    date_option=date_option,
                    selected_slots=[slot],
                )
                for slot in ranked_slots
            )
        selected_slots, missing = self.choose_slots(
            slots=live_slots,
            time_slots=time_slots,
            preferred_fields=preferred_fields,
            slot_selection_mode=slot_selection_mode,
        )
        if missing:
            wanted = ", ".join(missing)
            if slot_selection_mode == SLOT_SELECTION_MODE_FIRST_AVAILABLE:
                raise SportsAPIError(f"No selectable slots are available across preferred fallback times: {wanted}")
            raise SportsAPIError(f"No selectable slots are available for: {wanted}")
        return self._build_reservation_preview(
            venue_id=venue_id,
            motion_type=prepared_reservation.motion_type,
            date_option=date_option,
            selected_slots=selected_slots,
            fallback_candidates=fallback_candidates,
        )

    def build_cron_preview(
        self,
        *,
        venue_id: str,
        motion: str,
        time_slots: Iterable[str],
        preferred_fields: Iterable[str] | None = None,
        slot_selection_mode: str = SLOT_SELECTION_MODE_ALL_REQUIRED,
        window_start_days: int = 0,
        window_end_days: int = 7,
        redeem_deadline_hours: int = 2,
        now: datetime | None = None,
    ) -> ReservationPreview:
        motion_type = self.resolve_motion_type(venue_id, motion)
        date_options = self.list_date_options(venue_id, motion_type.id)
        reference_now = now or datetime.now()
        window_start = reference_now.date() + timedelta(days=window_start_days)
        window_end = reference_now.date() + timedelta(days=window_end_days)
        requested_time_slots = list(time_slots)
        saw_date_in_window = False
        cutoff_filtered = False

        for date_option in date_options:
            date_value = datetime.strptime(date_option.date, "%Y-%m-%d").date()
            if date_value < window_start or date_value > window_end:
                continue
            saw_date_in_window = True
            live_slots = self.list_available_slots(
                venue_id,
                motion_type.id,
                date=date_option.date,
                date_id=date_option.date_id,
            )
            filtered_slots: list[FieldSlot] = []
            for slot in live_slots:
                if slot.time_slot not in requested_time_slots:
                    continue
                slot_start = self._slot_start_at(date_option.date, slot.time_slot)
                if slot_start - reference_now < timedelta(hours=redeem_deadline_hours):
                    cutoff_filtered = True
                    continue
                filtered_slots.append(slot)

            fallback_candidates: tuple[ReservationAttemptCandidate, ...] = ()
            if slot_selection_mode == SLOT_SELECTION_MODE_FIRST_AVAILABLE:
                ranked_slots, missing = self.rank_slot_candidates(
                    slots=filtered_slots,
                    time_slots=requested_time_slots,
                    preferred_fields=preferred_fields,
                )
                if not ranked_slots:
                    continue
                fallback_candidates = tuple(
                    self._build_attempt_candidate(
                        venue_id=venue_id,
                        motion_type=motion_type,
                        date_option=date_option,
                        selected_slots=[slot],
                    )
                    for slot in ranked_slots
                )

            selected_slots, missing = self.choose_slots(
                slots=filtered_slots,
                time_slots=requested_time_slots,
                preferred_fields=preferred_fields,
                slot_selection_mode=slot_selection_mode,
            )
            if missing:
                continue

            return self._build_reservation_preview(
                venue_id=venue_id,
                motion_type=motion_type,
                date_option=date_option,
                selected_slots=selected_slots,
                fallback_candidates=fallback_candidates,
            )

        if not saw_date_in_window:
            raise SportsAPIError(
                f"No reservable dates are currently exposed between {window_start.isoformat()} and {window_end.isoformat()}."
            )
        if cutoff_filtered:
            raise SportsAPIError("Matching slots exist but are already past the redeem deadline.")
        raise SportsAPIError(
            "No selectable slots are available in the configured cron window for the requested time slots."
        )

    @staticmethod
    def build_selected_spaces(slots: Iterable[FieldSlot]) -> list[dict[str, Any]]:
        selected = []
        for slot in slots:
            selected.append(
                {
                    "venuePrice": slot.price,
                    "count": slot.count,
                    "sign": slot.sign,
                    "status": 1,
                    "scheduleTime": slot.time_slot,
                    "subSitename": slot.field_name,
                    "subSiteId": slot.field_id,
                    "tensity": slot.raw.get("tensity") or slot.raw.get("tension") or "1",
                    "venueNum": 1,
                }
            )
        return selected

    def select_slots(
        self,
        venue_id: str,
        motion_type_id: str,
        *,
        date: str,
        date_id: str,
        field_names: Iterable[str],
        time_slots: Iterable[str],
    ) -> list[FieldSlot]:
        wanted_fields = set(field_names)
        wanted_times = set(time_slots)
        slots = self.list_available_slots(
            venue_id,
            motion_type_id,
            date=date,
            date_id=date_id,
        )
        selected = [
            slot
            for slot in slots
            if slot.field_name in wanted_fields and slot.time_slot in wanted_times
        ]
        missing = [
            f"{field_name} {time_slot}"
            for field_name in wanted_fields
            for time_slot in wanted_times
            if not any(
                slot.field_name == field_name and slot.time_slot == time_slot
                for slot in selected
            )
        ]
        if missing:
            raise SportsAPIError(
                "Requested slots are not currently selectable: " + ", ".join(sorted(missing))
            )
        return selected

    def get_terms(self) -> dict[str, Any]:
        return self._json_request(
            "POST",
            "/venue/moreSetting/getOne",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data="",
        )

    def get_slider_captcha(self, client_uid: str | None = None) -> dict[str, Any]:
        payload = self._json_request(
            "POST",
            "/captcha/get",
            json={
                "captchaType": "blockPuzzle",
                "clientUid": client_uid or self._build_client_uid(),
                "ts": int(time.time() * 1000),
            },
        )
        return payload

    def build_captcha_verification(
        self,
        token: str,
        point_x: int,
        *,
        secret_key: str | None = None,
        point_y: int = 5,
    ) -> str:
        point_json = json.dumps({"x": point_x, "y": point_y}, ensure_ascii=False, separators=(",", ":"))
        if secret_key:
            point_json = self._aes_encrypt(point_json, secret_key)
            return self._aes_encrypt(f"{token}---{point_json}", secret_key)
        return f"{token}---{point_json}"

    def verify_slider_captcha(
        self,
        token: str,
        point_x: int,
        *,
        secret_key: str | None = None,
        point_y: int = 5,
    ) -> dict[str, Any]:
        point_json = json.dumps({"x": point_x, "y": point_y}, ensure_ascii=False, separators=(",", ":"))
        if secret_key:
            point_json = self._aes_encrypt(point_json, secret_key)
        return self._json_request(
            "POST",
            "/captcha/check",
            json={
                "captchaType": "blockPuzzle",
                "pointJson": point_json,
                "token": token,
            },
        )

    def build_confirm_order_payload(
        self,
        *,
        venue_id: str,
        motion_type: MotionType,
        date_option: DateOption,
        selected_spaces: list[dict[str, Any]],
        return_url: str = DEFAULT_RETURN_URL,
    ) -> dict[str, Any]:
        return {
            "venTypeId": motion_type.id,
            "venueId": venue_id,
            "fieldType": motion_type.name,
            "returnUrl": return_url,
            "scheduleDate": date_option.date,
            "week": date_option.week,
            "spaces": selected_spaces,
            "tenSity": self._tension_label(motion_type.tension),
            "dateId": date_option.date_id,
        }

    def confirm_personal_order(
        self,
        payload: dict[str, Any],
        *,
        captcha_verification: str | None = None,
    ) -> dict[str, Any]:
        aes_key = self._random_aes_key()
        timestamp = str(int(time.time() * 1000))
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "sid": self._rsa_encrypt(aes_key),
            "tim": self._rsa_encrypt(timestamp),
        }
        if captcha_verification:
            headers["tid"] = captcha_verification
        encrypted_payload = self._aes_encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            aes_key,
        )
        return self._json_request(
            "POST",
            "/venue/personal/ConfirmOrder",
            headers=headers,
            data=encrypted_payload,
        )

    def get_order_details(self, order_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/venue/personal/queryOrder?orderId={order_id}")

    def create_payment(self, order_id: str) -> dict[str, Any]:
        payload = self._json_request(
            "POST",
            "/venue/personal/orderImmediatelyPC",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=self._encode_form({"orderId": order_id}),
        )
        payment_data = payload.get("data")
        payment_url = None
        if payment_data and payment_data != 3:
            payment_url = (
                "https://cwc2.jdcw.sjtu.edu.cn/payment/pay/pay.action"
                f"?data={payment_data['data']}"
                f"&sign={payment_data['sign']}"
                f"&subsysid={payment_data['subsysid']}"
                f"&sysid={payment_data['sysid']}"
            )
        return {
            "code": payload.get("code"),
            "msg": payload.get("msg"),
            "data": payment_data,
            "payment_url": payment_url,
        }

    def preview_personal_order(
        self,
        *,
        venue_id: str,
        motion: str,
        date: str,
        field_names: Iterable[str],
        time_slots: Iterable[str],
    ) -> dict[str, Any]:
        motion_type = self.resolve_motion_type(venue_id, motion)
        date_options = self.list_date_options(venue_id, motion_type.id)
        date_map = {option.date: option for option in date_options}
        if date not in date_map:
            raise SportsAPIError(f"Date {date} is not currently exposed for venue {venue_id}.")
        selected_slots = self.select_slots(
            venue_id,
            motion_type.id,
            date=date,
            date_id=date_map[date].date_id,
            field_names=field_names,
            time_slots=time_slots,
        )
        preview = self._build_reservation_preview(
            venue_id=venue_id,
            motion_type=motion_type,
            date_option=date_map[date],
            selected_slots=selected_slots,
        )
        return {
            "motion_type": preview.motion_type,
            "date_option": preview.date_option,
            "selected_slots": list(preview.selected_slots),
            "selected_spaces": list(preview.selected_spaces),
            "confirm_order_payload": preview.confirm_order_payload,
            "total_price": preview.total_price,
        }

    @staticmethod
    def _tension_label(tension: int | None) -> str:
        return {
            0: "正常",
            1: "紧张",
            2: "很紧张",
            3: "非常紧张",
            4: "很紧张(签退)",
        }.get(tension, "正常")
