from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin

class DoorClient (OAuthClientBase):
    """Client for the SJTU dormitory door opening service."""
    def __init__(self, room_id: str, session_file="door_client.cookies"):
        super().__init__("DoorClient", session_file)
        self.room_id = room_id
        self.base_url = f"https://door.sjtu.edu.cn/ui?roomid={room_id}"

    def get_truncated_room_id(self):
        return self.room_id[:-8]

    def open_door(self, jac_login: JACLogin):
        """Open the door."""
        self.logger.info("Opening the door.")
        response = self.session.get(self.base_url, allow_redirects=False)
        if response.status_code == 302:
            login_url = response.headers["Location"]
            self.logger.debug("Login required, performing login.")
            final_redirect_url = jac_login.login(login_url)
            # final_redirect_url = final_redirect_url.replace("http://", "https://")
            self.session.get(final_redirect_url, allow_redirects=True)
        response = self.session.get(
            f"https://door.sjtu.edu.cn/api/key?roomid={self.get_truncated_room_id()}"
            )
        # success: {"errno":200,"error":"{\"code\":200,\"data\":\"远程开门指令处理完成\",\"operateId\":xxx,\"requestId\":\"xxx\",\"message\":\"OK\"}","total":0}
        # failire: {"errno":403,"error":"你没有权限开启此门！","total":0}
        if response.json()["errno"] == 200:
            self.logger.info("Door opened successfully.")
            self.save_session()
        else:
            error_message = response.json()["error"]
            self.logger.error(f"Failed to open the door: {error_message}")

    def get_room_name(self):
        """Get the room name."""
        response = self.session.get(
            f"https://door.sjtu.edu.cn/api/key/roomname?roomid={self.get_truncated_room_id()}"
        )
        # success: {"errno":200,"error":"success","total":1,"entities":["xx校区-xx宿舍-xxx"]}
        # failure: {"errno":200,"error":"success","total":1,"entities":["房间号输入错误！"]}
        return response.json()["entities"][0] if response.status_code == 200 else None

if __name__ == "__main__":
    import log
    import logging
    from jac_login import get_test_jac_login

    # Configure the root logger for demo purposes
    log.DEFAULT_LOG_LEVEL = logging.DEBUG

    logger = get_logger("door_client_test", level=logging.DEBUG)

    # get room id from test/room_id.txt
    with open("test/room_id.txt", "r") as f:
        room_id = f.read().strip()

    door_client = DoorClient(room_id)
    jac_login = get_test_jac_login()
    door_client.open_door(jac_login)

    # get room name
    room_name = door_client.get_room_name()
    if room_name:
        logger.info(f"Room name: {room_name}")
    else:
        logger.error("Failed to get room name.")