import random
import math
import json
import uuid
from enum import Enum
from oauth_client import OAuthClientBase
from jac_login import JACLogin
from datetime import datetime, timezone, timedelta
from time import sleep

class LocationType(Enum):
    """Predefined location types for running"""
    DEFAULT = (121.426100000000000, 31.025860000000000, 121.443170000000000, 31.0311700000000000)

    def __init__(self, start_lon, start_lat, end_lon, end_lat):
        self.start_lon = start_lon
        self.start_lat = start_lat
        self.end_lon = end_lon
        self.end_lat = end_lat

class PEClient(OAuthClientBase):
    """Client for SJTU PE system running"""
    
    def __init__(self, jac_login: JACLogin, session_file="pe_client.cookies"):
        super().__init__("PEClient", session_file)
        self.base_url = "https://pe.sjtu.edu.cn"
        self.jac_login = jac_login
        self.uid = None
        self.set_user_agent("mobile")

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

    def generate_location(self, base_lng=121.426100000000000, base_lat=31.025860000000000) -> tuple[float, float]:
        """Generate random location within 5m of base point"""
        radius_meters = 5
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

    def upload_result(self, data: dict, lon=121.426100000000000, lat=31.025860000000000):
        """Upload running result"""
        
        if not self.uid:
            self.logger.error("UID is not set. Cannot upload result.")
            return None
        
        headers_to_send = {
            "Authorization": self.uid
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

    def simulate_running(self, run_time=None, location_type=LocationType.DEFAULT):
        """
        Simulate complete running process with customizable parameters
        
        Args:
            run_time: Datetime when the run ended (default: current time)
            location_type: Type of location from LocationType enum (default: DEFAULT)
        
        Returns:
            API response from server
        """
        print("Logging in...")
        if not self.login():
            print("Login failed")
            return None
        
        try:
            uid = self.get_uid()
            print(f"Got UID: {uid}")
        except Exception as e:
            print(f"Failed to get UID: {e}")
            uid = "TEST_UID_12345"
        
        if run_time is None:
            now = datetime.now()
        else:
            now = run_time
            
        chinese_time = now.strftime("%Y年%m月%d日 %H:%M")
        iso_time = now.strftime("%Y-%m-%d %H:%M")
        
        start_lon, start_lat = self.generate_location(location_type.start_lon, location_type.start_lat)
        end_lon, end_lat = self.generate_location(location_type.end_lon, location_type.end_lat)
        
        location1 = f"{start_lon},{start_lat}"
        location2 = f"{end_lon},{end_lat}"
        
        base_time = now - timedelta(minutes=10)
        end_time = now
        
        points = [
            {
            "locatetime": int(base_time.timestamp()) * 1000,
            "location": location1,
            "seconds": 1
            },
            {
            "locatetime": int(end_time.timestamp()) * 1000,
            "location": location2,
            "seconds": 599
            }
        ]
        
        tracks = [{
            "counts": 2,
            "trid": str(uuid.uuid4()).upper(),
            "duration": 600,
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
        
        self.logger.info(f"Generated result data: {result_data}")
        response = self.upload_result(result_data[0], location_type.start_lon, location_type.start_lat)
        return response


if __name__ == "__main__":
    from jac_login import get_test_jac_login
    
    def demo_simulate_running():
        """Demo function for one-click simulation"""
        jac_login = get_test_jac_login()
        client = PEClient(jac_login)
        
        yesterday = datetime.now() - timedelta(days=7)
        client.simulate_running(run_time=yesterday, location_type=LocationType.DEFAULT)

    demo_simulate_running()
