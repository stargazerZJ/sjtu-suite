from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin


def get_truncated_room_id(room_id: str):
    """The last 8 digits of the room id seems to be a date."""
    return room_id[:-8]


class DoorClient(OAuthClientBase):
    """Client for the SJTU dormitory door opening service."""

    def __init__(self, room_id: str, jac_login: JACLogin, session_file="door_client.cookies"):
        super().__init__("DoorClient", session_file)
        self.room_id = room_id
        self.base_url = f"https://door.sjtu.edu.cn"
        self.jac_login = jac_login

    def login(self):
        """Login to the door opening service."""
        self.logger.debug("Performing login.")
        response = self.session.get(self.base_url + "/ui", allow_redirects=False)
        if response.status_code == 302:
            login_url = response.headers["Location"]
            self.logger.debug("Login required, performing login.")
            final_redirect_url = self.jac_login.login(login_url)
            # final_redirect_url = final_redirect_url.replace("http://", "https://")
            self.session.get(final_redirect_url, allow_redirects=False)
            response = self.session.get(self.base_url + "/ui", allow_redirects=False)
            if response.status_code == 200:
                self.logger.info("Login session acquired or refreshed.")
                self.save_session()
                return True
            else:
                self.logger.error("Login failed.")
                return False

    def open_door(self, room_id: str = ""):
        """Open the door."""
        room_id = room_id or self.room_id
        self.logger.debug("Opening the door.")
        response = self.session.get(
            self.base_url + f"/api/key?roomid={get_truncated_room_id(room_id)}"
        )
        # success: {"errno":200,"error":"{\"code\":200,\"data\":\"远程开门指令处理完成\",\"operateId\":xxx,\"requestId\":\"xxx\",\"message\":\"OK\"}","total":0}
        # failire: {"errno":403,"error":"你没有权限开启此门！","total":0}
        if response.json()["errno"] == 200:
            self.logger.info("Door opened successfully.")
            self.save_session()
            return True
        else:
            error_message = response.json()["error"]
            self.logger.error(f"Failed to open the door: {error_message}")
            return False

    def get_room_name(self, room_id: str = ""):
        """Get the room name."""
        room_id = room_id or self.room_id
        response = self.session.get(
            self.base_url + f"/api/key/roomname?roomid={get_truncated_room_id(room_id)}"
        )
        # success: 200, {"errno":200,"error":"success","total":1,"entities":["xx校区-xx宿舍-xxx"]}
        # failure: 200, {"errno":200,"error":"success","total":1,"entities":["房间号输入错误！"]}
        # failure: 401, {"errno":10004,"error":"USER_LOGIN_REQUIRED","total":0}
        return response.json()["entities"][0] if response.status_code == 200 else None


if __name__ == "__main__":
    import log
    import logging
    from jac_login import get_test_jac_login

    # Configure the root logger for demo purposes
    log.DEFAULT_LOG_LEVEL = logging.DEBUG

    logger = get_logger("door_client_test", level=logging.DEBUG)

    # get room id from test/room_id.txt
    with open("room_id.txt", "r") as f:
        room_id = f.read().strip()

    jac_login = get_test_jac_login()
    door_client = DoorClient(room_id, jac_login)
    door_client.login()
    door_client.open_door()

    # get room name
    room_name = door_client.get_room_name()
    if room_name:
        logger.info(f"Room name: {room_name}")
    else:
        logger.error("Failed to get room name.")
