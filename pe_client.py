import random
import math
from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin
from datetime import datetime, timezone

class PEClient(OAuthClientBase):
    """Client for SJTU PE system running"""
    
    def __init__(self, jac_login: JACLogin, session_file="pe_client.cookies"):
        super().__init__("PEClient", session_file)
        self.base_url = "https://pe.sjtu.edu.cn"
        self.jac_login = jac_login
        self.uid = None

    def validate_session(self) -> tuple[bool, str]:
        """Check if session is valid"""
        response = self.session.get(f"{self.base_url}/login")
        redir_url = response.headers.get("Location")
        if redir_url and redir_url != "/index":
            self.logger.debug("Session is invalid, redirecting to login.")
            return False, redir_url
        self.logger.debug("Session is valid.")
        return True, None

    def login(self):
        """Login to PE system"""
        need_login, login_url = self.validate_session()
        
        if not need_login:
            self.logger.debug("Already logged in.")
            return True
        
        final_url = self.jac_login.login(login_url)
        self.session.get(final_url)
        self.save_session()
        return self.validate_session()

    def get_uid(self) -> str:
        """Get user UID from PE system"""
        if self.uid:
            return self.uid
            
        response = self.session.get(f"{self.base_url}/sports/my/uid")
        data = response.json()
        if data.get("code") == 0:
            self.uid = data["data"]["uid"]
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
        return self.session.get(f"{self.base_url}/api/running/point-rule?location={location}")

    def upload_result(self, data: dict):
        """Upload running result"""
        headers = {
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/sports"
        }
        return self.session.post(
            f"{self.base_url}/api/running/result/upload",
            json=data,
            headers=headers
        )

    def example_result_data(self) -> dict:
        """Generate example result data"""
        lng, lat = self.generate_location()
        return {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
            "location": {
                "longitude": lng,
                "latitude": lat,
                "accuracy": 10.0
            },
            "duration": 1800,
            "distance": 3000,
        }


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
        
        now = datetime.now()
        chinese_time = now.strftime("%Y年%m月%d日 %H:%M")
        iso_time = now.strftime("%Y-%m-%d %H:%M:%S")
        
        tracks = []
        for i in range(2):
            point_count = random.randint(100, 500)
            duration = random.randint(4, 15)
            distance = random.uniform(10.0, 30.0)
            
            points = []
            base_time = now - timedelta(minutes=30)
            for j in range(point_count):
                loc_time = base_time + timedelta(seconds=j)
                lng, lat = client.generate_location()
                points.append({
                    "locatetime": int(loc_time.timestamp() * 1000),
                    "location": f"{lng:.15f},{lat:.15f}",
                    "seconds": j
                })
            
            tracks.append({
                "counts": point_count,
                "trid": str(uuid.uuid4()).upper(),
                "duration": duration,
                "points": points,
                "status": "normal",
                "distance": distance
            })
        
        result_data = [{
            "time": chinese_time,
            "vaildDistance": f"{sum(t['distance'] for t in tracks):.2f}",
            "sumDistance": f"{sum(t['distance'] for t in tracks) * 1.2:.2f}",
            "uid": uid,
            "spavg": 0,
            "sid": str(uuid.uuid4()).upper(),
            "updateTime": iso_time,
            "type": "score",
            "fravg": 0,
            "tracks": tracks,
            "state": False
        }]
        
        print("Generated running data:")
        print(json.dumps(result_data, indent=2, ensure_ascii=False))
        
        print("\nUploading result...")
        response = client.upload_result(result_data[0])
        print(f"Upload response: {response.status_code}")
        print(response.text)

    simulate_running()
