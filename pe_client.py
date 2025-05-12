import random
import math
from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin
from datetime import datetime, timezone
from time import sleep

class PEClient(OAuthClientBase):
    """Client for SJTU PE system running"""
    
    def __init__(self, jac_login: JACLogin, session_file="pe_client.cookies"):
        super().__init__("PEClient", session_file)
        self.base_url = "https://pe.sjtu.edu.cn"
        self.jac_login = jac_login
        self.uid = None

    def validate_session(self) -> bool:
        """Check if session is valid"""
        response = self.session.get(f"{self.base_url}/index", allow_redirects=False)
        response.headers.get("Location")
        if response.status_code == 302:
            self.logger.debug("Session is invalid, redirecting to login.")
            return False
        self.logger.debug("Session is valid.")
        return True

    def login(self):
        """Login to PE system"""
        session_stat = self.validate_session()
        
        if session_stat:
            self.logger.debug("Already logged in.")
            return True
        
        login_url = self.session.get(f"{self.base_url}/login", allow_redirects=False).headers["Location"]
        final_redirect_url = self.jac_login.login(login_url)
        self.session.get(final_redirect_url, allow_redirects=False)
        response = self.session.get(self.base_url + "/index", allow_redirects=False)
        if response.status_code == 200:
            self.logger.info("Login session acquired or refreshed.")
            self.save_session()
            return True
        else:
            self.logger.error("Login failed.")
            return False

    def get_uid(self) -> str:
        """Get user UID from PE system"""
        response = self.session.get(f"{self.base_url}/sports/my/uid", allow_redirects=False)
        if response.status_code == 302:
            self.login()
        data = response.json()
        if data.get("code") == 0:
            self.uid = data.get("data").get("uid")
            return self.uid
        raise Exception("Failed to get UID")

    def generate_location(self, base_lng=121.4347607421875, base_lat=31.024383680555555) -> tuple[float, float]:
        """Generate random location within 100m of base point"""
        radius_meters = 100
        radius_deg = radius_meters / 111320.0
        angle = random.uniform(0, 2 * math.pi)
        radius = random.uniform(0, radius_deg)
        new_lng = base_lng + radius * math.cos(angle)
        new_lat = base_lat + radius * math.sin(angle)
        return new_lng, new_lat

    def get_point_rule(self, lng: float, lat: float):
        """Get running point rules for location"""
        location = f"{lng}%2C{lat}"
        headers = {
            "Authorization": self.uid
        }
        print(f"{self.base_url}/api/running/point-rule?location={location}")
        return self.session.get(f"{self.base_url}/api/running/point-rule?location={location}", headers=headers)

    def upload_result(self, data: dict, lon = 121.4347607421875, lat = 31.024383680555555):
        """Upload running result"""
        user_agent_string = "TaskCenterApp/3.4.5/iPhone 13/ScreenFringe (iOS,iPhone,18.1.1; Scale/3.0)"
        
        if not self.uid:
            self.logger.error("UID is not set. Cannot upload result.")
            return None

        headers_to_send = {
            "Authorization": self.uid,
            "User-Agent": user_agent_string
        }

        point_rule_response = self.get_point_rule(lon, lat)
        self.logger.info(f"Point rule response status: {point_rule_response.status_code}")
        self.logger.info(f"Point rule response text: {point_rule_response.text}")

        if point_rule_response.status_code != 200:
            self.logger.error("Failed to get point rule, aborting upload.")
            return point_rule_response

        sleep(25)

        json_payload_string = json.dumps([data], ensure_ascii=False)
        
        headers_to_send['Content-Type'] = 'application/json; charset=utf-8'

        self.logger.info(f"Attempting to upload result with UID: {self.uid}")
        self.logger.debug(f"Sending JSON payload: {json_payload_string}")

        response = self.session.post(
            f"{self.base_url}/api/running/result/upload",
            data=json_payload_string.encode('utf-8'),
            headers=headers_to_send
        )

        self.logger.info(f"Upload response status: {response.status_code}")
        self.logger.info(f"Upload response text: {response.text}")

        return response



if __name__ == "__main__":
    import uuid
    from datetime import datetime, timedelta
    import json
    from jac_login import get_test_jac_login
    
    def simulate_running():
        """Simulate complete running process"""
        jac_login = get_test_jac_login()
        client = PEClient(jac_login)
        
        print("Logging in...")
        if not client.login():
            print("Login failed")
            return
        
        try:
            uid = client.get_uid()
            print(f"Got UID: {uid}")
        except Exception as e:
            print(f"Failed to get UID: {e}")
            uid = "TEST_UID_12345"
        
        now = datetime.now() - timedelta(hours=1)
        chinese_time = now.strftime("%Y年%m月%d日 %H:%M")
        iso_time = now.strftime("%Y-%m-%d %H:%M")
        
        fixed_location1 = "121.43489203559028,31.0238313984375"
        fixed_location2 = "121.43489203559028,31.0741323984375"
        
        base_time = now - timedelta(minutes=30)
        end_time = now
        
        points = [
            {
            "locatetime": int(base_time.timestamp()) * 1000,
            "location": fixed_location1,
            "seconds": 1
            },
            {
            "locatetime": int(end_time.timestamp()) * 1000,
            "location": fixed_location2,
            "seconds": 1799
            }
        ]
        
        tracks = [{
            "counts": 2,
            "trid": str(uuid.uuid4()).upper(),
            "duration": 1800,
            "points": points,
            "status": "normal",
            "distance": 90.332086724487311
        }]
        
        result_data = [{
            "time": chinese_time,
            "vaildDistance": "3.20",
            "sumDistance": "5.59",
            "uid": uid,
            "spavg": 0,
            "sid": str(uuid.uuid4()).upper(),
            "updateTime": iso_time,
            "type": "score",
            "fravg": 0,
            "tracks": tracks,
            "state": True
        }]
        
        print("Generated running data:")
        print(json.dumps(result_data, indent=2, ensure_ascii=False))
        
        print("\nUploading result...")
        response = client.upload_result(result_data[0])
        print(f"Upload response: {response.status_code}")
        print(response.text)

    simulate_running()
