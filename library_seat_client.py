import itertools
from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin
from datetime import datetime, timedelta
import zoneinfo


class APIResponse:
    '''Every API response of the library seat system is of the same format.'''

    def __init__(self, response):
        response = response.json()
        self.code: int = response["code"]
        self.message: str = response["message"]
        self.data = response["data"]
        # self.count = response["count"]    # never needed
        # self.vals = response["vals"]


class ZoneInfo:
    '''Immutable data class for zone information.'''
    id: int
    name: str
    floor: str
    library: str
    seat_count: int
    seat_ids: list[int]
    seat_tag_prefix: str  # e.g. "W4-NW-043" -> "W4-NW"
    seat_tag_id_range: tuple[int, int]


class ReservationInfo:
    id: int
    seat_id: int
    start_time: datetime
    end_time: datetime
    has_checked_in: bool
    is_temporary_leaving: bool
    has_ended: bool
    infraction: bool


class LibrarySeatClient(OAuthClientBase):
    """Client for the SJTU library seat reservation system."""

    def __init__(self, jac_login: JACLogin, session_file="libseat_client.cookies", name="LibrarySeatClient"):
        super().__init__(name, session_file)
        self.base_url = f"https://libseat.sjtu.edu.cn"
        self.jac_login = jac_login
        self.user_id = self.get_user_id()

    def validate_session(self) -> bool:
        """Validate the login session."""
        response = self.session.get(self.base_url + "/ic-web/sysInfo?sysType=1&sysValue=1&sysKind=1")
        if response.json()["code"] == 0:
            return True
        else:
            # {"code":300,"message":"用户未登录，请重新登录", ... }
            return False

    def login(self):
        if self.validate_session():
            return
        self.do_login()

    def do_login(self):
        # Call /ic-web/auth/address?finalAddress=<homepage> to get the toLoginPage URL. No cookies needed.
        # -> toLoginPage (redirect URL /ic-web//auth/token?uuid=)
        # -> jaccount/authorize -> ...
        # -> libseat.sjtu.edu.cn/authcenter/doAuth/75d336ba09a7482cb87cbe8b7d915c98?code=...
        # -> /ic-web//auth/token?uuid= Retrieves ic-cookie=1db4f1f4-f4f5-4524-ab1d-d2e41da89fcd valid for about 1 hour
        # -> Homepage
        response = self.session.get(
            self.base_url + r"/ic-web/auth/address?finalAddress=https:%2F%2Flibseat.sjtu.edu.cn&errPageUrl=https:%2F%2Flibseat.sjtu.edu.cn%2F%23%2Ferror&manager=false&consoleType=16")
        response = APIResponse(response)
        location = response.data
        # location: https://libseat.sjtu.edu.cn/authcenter/toLoginPage?redirectUrl=...
        response = self.session.get(location, allow_redirects=False)
        location = response.headers["Location"]
        # location: https://jaccount.sjtu.edu.cn/jaccount/authorize?...
        location = self.jac_login.login(location)
        # location: https://libseat.sjtu.edu.cn/authcenter/doAuth/<uuid, hex of length 32>?code=...
        response = self.session.get(location, allow_redirects=False)
        location = response.headers["Location"]
        # location: https://libseat.sjtu.edu.cn/ic-web//auth/token?uuid=<uuid, hex of length 32>
        response = self.session.get(location, allow_redirects=False)
        # Now we have the session cookie.
        self.logger.info("Login session acquired or refreshed.")
        self.save_session()

    def call_API(self, path: str, method: str = "GET", data=None, params=None):
        """Call an API. Refresh the session if necessary."""
        response = self.session.request(method, self.base_url + path, json=data, params=params)
        response = APIResponse(response)
        if response.code == 300:
            self.do_login()
            response = self.session.request(method, self.base_url + path, json=data)
            response = APIResponse(response)
            # note: when the API does not exist, the response message is "系统错误，请联系管理员"
        return response

    def get_user_id(self):
        """Get the user ID."""
        response = self.call_API(f"/ic-web/auth/userInfo")
        return response.data["accNo"]

    def get_zone_mapping(self) -> dict[int, str]:
        """Get the mapping from zone ID to zone name."""
        response = self.call_API(f"/ic-web/seatMenu")

        zone_mapping = {}
        for library in response.data:
            for floor in library.get('children', []):
                for zone in floor.get('children', []):
                    if zone["totalCount"] > 0:
                        # Only include zones with available seats
                        zone_mapping[zone["id"]] = zone["name"]

        return zone_mapping

    def get_zone_info(self) -> dict[int, ZoneInfo]:
        """Get the mapping from zone ID to zone information."""
        response = self.call_API(f"/ic-web/seatMenu")

        zone_info = {}
        for library in response.data:
            for floor in library.get('children', []):
                for zone in floor.get('children', []):
                    if zone["totalCount"] > 0:
                        # Only include zones with available seats
                        info = ZoneInfo()
                        info.id = zone["id"]
                        info.name = zone["name"]
                        info.floor = floor["name"]
                        info.library = library["name"]
                        info.seat_count = zone["totalCount"]

                        response = self.call_API(f"/ic-web/reserve?roomIds={zone['id']}&resvDates=20240724&sysKind=8")
                        seat_ids = [seat["devId"] for seat in response.data if seat["devProp"] == 2]

                        info.seat_ids = seat_ids
                        # make sure that the total count is correct
                        assert zone["totalCount"] == len(seat_ids)

                        seat_tags = [seat["devName"] for seat in response.data if seat["devProp"] == 2]
                        info.seat_tag_prefix = seat_tags[0].rsplit("-", 1)[0]
                        seat_tag_ids = [int(seat_tag.rsplit("-", 1)[1]) for seat_tag in seat_tags]
                        info.seat_tag_id_range = (min(seat_tag_ids), max(seat_tag_ids))
                        # the seat tag IDs are continuous except for W6-SW-029
                        # assert len(seat_ids) == info.seat_tag_id_range[1] - info.seat_tag_id_range[0] + 1

                        zone_info[zone["id"]] = info

        return zone_info

    def get_seat_ids(self, zone_id: int) -> list[int]:
        """Get the list of seat IDs in a zone."""
        response = self.call_API(f"/ic-web/reserve?roomIds={zone_id}&resvDates=20240724&sysKind=8")
        # if the seat can be reserved, it's "devProp" is 2.
        return [seat["devId"] for seat in response.data if seat["devProp"] == 2]

    def get_seat_mapping(self) -> dict[str, int]:
        """Get the mapping from seat tag name to seat ID."""
        response = self.call_API(f"/ic-web/seatMenu")

        seat_mapping = {}
        for library in response.data:
            for floor in library.get('children', []):
                for zone in floor.get('children', []):
                    if zone["totalCount"] > 0:
                        response = self.call_API(f"/ic-web/reserve?roomIds={zone['id']}&resvDates=20240724&sysKind=8")
                        for seat in response.data:
                            if seat["devProp"] == 2:
                                seat_mapping[seat["devName"]] = seat["devId"]

        return seat_mapping

    def reserve_seat(self, seat_id: int, start_time: datetime = None, end_time: datetime = None):
        """Reserve a seat with default time period from now till 22:30 PM."""
        now = datetime.now(zoneinfo.ZoneInfo("Asia/Shanghai"))
        if start_time is None:
            start_time = now
        if end_time is None:
            end_time = now.replace(hour=22, minute=30, second=0, microsecond=0)

        response = self.call_API(f"/ic-web/reserve", "POST", {
            "sysKind": 8,
            "appAccNo": self.user_id,
            "memberKind": 1,
            "resvMember": [
                self.user_id
            ],
            "resvBeginTime": start_time.astimezone(zoneinfo.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "resvEndTime": end_time.astimezone(zoneinfo.ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "testName": "",
            "captcha": "",
            "resvProperty": 0,
            "resvDev": [
                seat_id
            ],
            "memo": ""
        })
        if response.code == 0:
            reservation_id = response.data["uuid"]
            # message is "新增成功"
            return True, reservation_id, response.message
        else:
            # code is 1
            # message can be:
            # - "设备在该时间段内已被预约"
            # - "学工号为：<your id>的用户在当前时段有预约"
            # - " 预订时间应该在60分钟到960分钟内"
            # - "不在提前预约时间范围内"
            # - "请在22:00后开始预约"   (if reserving seats for the next day)
            # - "预约时间要大于当前时间" (if start_time is >2min from now)
            # - "设备不在开放时间内"
            # - "请用户使用自己的账号预约"
            # - "预约设备信息错误"
            return False, None, response.message

    def cancel_reservation(self, reservation_id: int) -> tuple[bool, str]:
        """Cancel a reservation."""
        data = {"uuid": reservation_id}
        # If the reservation hasn't begun (<30min from start time), 'delete' should be called
        response = self.call_API(f"/ic-web/reserve/delete", "POST", data)
        if response.code == 0:
            # message is "删除成功"
            return True, response.message
        else:
            # message can be:
            # - "预约在当前状态下不能删除"
            pass
        # Otherwize, 'endAhaed'(misspelled) should be called
        response = self.call_API(f"/ic-web/reserve/endAhaed", "POST", data)
        if response.code == 0:
            # message is "操作成功"
            return True, response.message
        else:
            # - "预约已结束"
            return False, response.message

    def query_reservation(self):
        """Query the reservation."""
        response = self.call_API(f"/ic-web/reserve/resvInfo", params={
            "beginDate": datetime.today().strftime("%Y-%m-%d"),
            "endDate": (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d"),
            "needStatus": 6,  # status mask, 6 for not started or started but not ended
            # "needStatus": 4095,
            # if the two params below is unspecified, all results are given
            # "page": 1,
            # "pageNum": 10,
            "orderKey": "gmt_create",  # reservation creation time
            "orderModel": "desc"  # how poor English!
        })
        assert response.code == 0
        return [self.parse_reservation_info(info) for info in response.data]

    @staticmethod
    def parse_reservation_info(info: dict):
        reservation = ReservationInfo()
        reservation.id = info["uuid"]
        reservation.seat_id = info["resvDevInfoList"][0]["devId"]
        reservation.start_time = datetime.fromtimestamp(info["resvBeginTime"] / 1000)
        reservation.end_time = datetime.fromtimestamp(info["resvEndTime"] / 1000)
        status_mask = info["resvStatus"]
        reservation.has_checked_in = bool(status_mask & 64)
        reservation.is_temporary_leaving = bool(status_mask & 2048)
        reservation.has_ended = bool(status_mask & 128)
        reservation.infraction = bool(status_mask & 16)
        return reservation


if __name__ == "__main__":

    # Initialize logging
    logger = get_logger("LibrarySeatClient_Test")

    from jac_login import get_test_jac_login

    # Create JACLogin instance
    jac_login = get_test_jac_login()

    # Create LibrarySeatClient instance
    client = LibrarySeatClient(jac_login)

    # # Attempt to log in
    # client.login()

    # Validate the session
    if client.validate_session():
        logger.info("Login successful and session is valid.")
    else:
        logger.warning("Login failed or session is invalid.")

    # Test get_zone_mapping
    test_get_zone_mapping = False
    if test_get_zone_mapping:
        zone_mapping = client.get_zone_mapping()
        print("Zone mapping:", zone_mapping, sep="\n")

    # Test get_zone_info
    test_get_zone_info = False
    if test_get_zone_info:
        zone_info = client.get_zone_info()
        print("Zone info:")
        for zone in zone_info.values():
            print("id:", zone.id)
            print("name:", zone.name)
            print("floor:", zone.floor)
            print("library:", zone.library)
            print("seat_count:", zone.seat_count)
            print("seat_ids (head):", zone.seat_ids[:5])
            print("seat_tag_prefix:", zone.seat_tag_prefix)
            print("seat_tag_id_range:", zone.seat_tag_id_range)
            print()

    test_reservation = False
    if test_reservation:
        # Reserve the seat from now to 1 hour later
        _, reservation_id, message = client.reserve_seat(2340, datetime.now(), datetime.now() + timedelta(hours=1))
        print("Reservation ID:", reservation_id)
        print("Message:", message)

        # Query reservations
        reservation = client.query_reservation()
        print("Reservations:")
        for r in reservation:
            print("id:", r.id)
            print("seat_id:", r.seat_id)
            print("start_time:", r.start_time)
            print("end_time:", r.end_time)
            print("has_checked_in:", r.has_checked_in)
            print("is_temporary_leaving:", r.is_temporary_leaving)
            print("has_ended:", r.has_ended)
            print("infraction:", r.infraction)
            print()

        # Cancel the reservation
        if reservation_id is not None:
            _, message = client.cancel_reservation(reservation_id)
            print("Message:", message)

    test_seat_mapping = False
    if test_seat_mapping:
        seat_mapping = client.get_seat_mapping()
        print("Seat mapping:")
        for key, value in itertools.islice(seat_mapping.items(), 20):
            print(f"{key}: {value}")
