import random
import math
import json
import uuid
import os
from enum import Enum
from sjtusuite.auth import OAuthClientBase, JACLogin
from sjtusuite.core.config import get_project_root
from datetime import datetime, timezone, timedelta
from time import sleep
from pathlib import Path


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

    @staticmethod
    def _parse_json_response(response):
        try:
            return response.json()
        except ValueError:
            return None

    def _is_identity_error(self, response) -> bool:
        data = self._parse_json_response(response)
        return bool(data and data.get("code") == 1003)

    def clear_pe_session(self):
        """Remove PE-domain cookies so the next login starts a fresh PE session."""
        for cookie in list(self.session.cookies):
            if cookie.domain == "pe.sjtu.edu.cn" or cookie.domain.endswith(".pe.sjtu.edu.cn"):
                self.session.cookies.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
        self.uid = None

    def relogin(self):
        """Force a fresh PE login session."""
        self.clear_pe_session()
        return self.login()

    def login(self):
        """Login to PE system"""
        # The uid returned by /sports/my/uid is scoped to the current PE session.
        self.uid = None
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
            if not self.login():
                raise Exception("Failed to refresh PE login session")
            response = self.session.get(f"{self.base_url}/sports/my/uid", allow_redirects=False)
        data = response.json()
        if data.get("code") == 0:
            self.uid = data.get("data").get("uid")
            return self.uid
        raise Exception("Failed to get UID")

    def generate_location(self, base_lng, base_lat) -> tuple[float, float]:
        """Generate random location within 0.02m of base point"""
        radius_meters = 0.02
        radius_deg = radius_meters / 111320.0
        angle = random.uniform(0, 2 * math.pi)
        radius = random.uniform(0, radius_deg)
        new_lng = base_lng + radius * math.cos(angle)
        new_lat = base_lat + radius * math.sin(angle)
        return new_lng, new_lat

    def get_point_rule(self, lng: float, lat: float, retry_on_identity_error: bool = True):
        """Get running point rules for location"""
        if not self.uid:
            self.get_uid()
        location = f"{lng}%2C{lat}"
        headers = {"Authorization": self.uid}
        response = self.session.get(f"{self.base_url}/api/running/point-rule?location={location}", headers=headers)
        if retry_on_identity_error and self._is_identity_error(response):
            self.logger.warning("PE identity token was rejected during point-rule lookup. Re-establishing the PE session and retrying.")
            if self.relogin():
                headers["Authorization"] = self.get_uid()
                response = self.session.get(f"{self.base_url}/api/running/point-rule?location={location}", headers=headers)
            if self._is_identity_error(response) and self.relogin():
                headers["Authorization"] = self.get_uid()
                response = self.session.get(f"{self.base_url}/api/running/point-rule?location={location}", headers=headers)
        return response

    def upload_result(self, data: dict, lon, lat, retry_on_identity_error: bool = True):
        """Upload running result"""
        
        if not self.uid:
            self.logger.error("UID is not set. Cannot upload result.")
            return None
        
        headers_to_send = {
            "Authorization": self.uid
        }

        point_rule_response = self.get_point_rule(lon, lat, retry_on_identity_error=retry_on_identity_error)
        self.logger.info(f"Point rule response status: {point_rule_response.status_code}")
        self.logger.info(f"Point rule response text: {point_rule_response.text}")

        if point_rule_response.status_code != 200:
            self.logger.error("Failed to get point rule, aborting upload.")
            return point_rule_response

        sleep(2)

        json_payload_string = json.dumps([data], ensure_ascii=False)
        
        headers_to_send["Authorization"] = self.uid
        headers_to_send['Content-Type'] = 'application/json; charset=utf-8'

        self.logger.info(f"Attempting to upload result with UID: {self.uid}")
        self.logger.debug(f"Sending JSON payload: {json_payload_string}")

        response = self.session.post(
            f"{self.base_url}/api/running/result/upload",
            data=json_payload_string.encode('utf-8'),
            headers=headers_to_send
        )

        if retry_on_identity_error and self._is_identity_error(response):
            self.logger.warning("PE identity token was rejected during upload. Re-establishing the PE session and retrying once.")
            if self.relogin():
                headers_to_send["Authorization"] = self.get_uid()
            point_rule_response = self.get_point_rule(lon, lat, retry_on_identity_error=False)
            self.logger.info(f"Point rule response status: {point_rule_response.status_code}")
            self.logger.info(f"Point rule response text: {point_rule_response.text}")
            response = self.session.post(
                f"{self.base_url}/api/running/result/upload",
                data=json_payload_string.encode('utf-8'),
                headers=headers_to_send
            )

        self.logger.info(f"Upload response status: {response.status_code}")
        self.logger.info(f"Upload response text: {response.text}")

        return response

    def load_points_data(self, points_file="data/points.json"):
        """Load points data from JSON file"""
        try:
            # Use project root instead of script directory
            file_path = get_project_root() / points_file
            
            with open(file_path, "r") as f:
                points_data = json.load(f)
            
            self.logger.info(f"Loaded {len(points_data)} points from {points_file}")
            return points_data
        except Exception as e:
            self.logger.error(f"Failed to load points data: {e}")
            return []

    def simulate_running(self, run_time=None, n=15000):
        """
        Simulate complete running process with customizable parameters
        
        Args:
            run_time: Datetime when the run ended (default: current time)
            n: Number of continuous points to use (default: 15000)
        
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
        
        all_points = self.load_points_data()
        
        # If no points found or n is too large, fallback to original method
        if not all_points or len(all_points) < n:
            self.logger.warning(f"Not enough points in points.json (needed {n}, found {len(all_points)}). Falling back to basic simulation.")
            start_lon, start_lat = self.generate_location(LocationType.DEFAULT.start_lon, LocationType.DEFAULT.start_lat)
            end_lon, end_lat = self.generate_location(LocationType.DEFAULT.end_lon, LocationType.DEFAULT.end_lat)
            
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
            total_distance = 1730.332086724487311
            total_duration = 600
            
        else:
            # Select a random starting point for n continuous points
            if len(all_points) > n:
                start_idx = random.randint(0, len(all_points) - n)
                selected_points = all_points[start_idx:start_idx + n]
            else:
                selected_points = all_points
            
            total_duration = selected_points[-1]["seconds"] - selected_points[0]["seconds"]
            
            end_time = now
            start_time = end_time - timedelta(seconds=total_duration)
            processed_points = []
            
            for point in selected_points:
                point_copy = point.copy()
                point_copy["locatetime"] = int(start_time.timestamp() + point["seconds"]) * 1000
                point_copy["seconds"] = point["seconds"] - selected_points[0]["seconds"]
                original_location = point["location"].split(",")
                new_location = self.generate_location(float(original_location[0]), float(original_location[1]))
                point_copy["location"] = f"{new_location[0]},{new_location[1]}"
                processed_points.append(point_copy)
            
            points = processed_points
            
            total_distance = 0.25 * total_duration / 60  # Simple estimate: ~15km per hour
            
            self.logger.info(f"Generated track with {len(points)} points spanning {total_duration} seconds")
        
        tracks = [{
            "counts": len(points),
            "trid": str(uuid.uuid4()).upper(),
            "duration": total_duration,
            "points": points,
            "status": "normal",
            "distance": f"{(total_distance * 1000 * 1.05):.15f}"
        }]
        
        result_data = [{
            "time": chinese_time,
            "vaildDistance": f"{(total_distance):.2f}",  # Convert to km
            "sumDistance": f"{(total_distance*1.05):.2f}",  # Slightly higher than valid distance
            "uid": uid,
            "spavg": 0,
            "sid": str(uuid.uuid4()).upper(),
            "updateTime": iso_time,
            "type": "score",
            "fravg": 0,
            "tracks": tracks,
            "state": True
        }]
        start_location = points[0]["location"].split(",")
        lon, lat = float(start_location[0]), float(start_location[1])
        
        self.logger.info(f"Generated result data with {len(points)} points")
        response = self.upload_result(result_data[0], lon, lat)
        return response


if __name__ == "__main__":
    from sjtusuite.auth import get_test_jac_login
    
    def demo_simulate_running():
        """Demo function for one-click simulation"""
        jac_login = get_test_jac_login()
        client = PEClient(jac_login)
        
        nowtime = datetime.now() - timedelta(minutes=30)
        client.simulate_running(run_time=nowtime, n=10000)

    demo_simulate_running()
