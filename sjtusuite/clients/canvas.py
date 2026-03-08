"""
SJTU Canvas Client - Fetch enrolled courses from Canvas LMS.

Uses Canvas API (oc.sjtu.edu.cn) to list courses for the authenticated user.
Requires JAccount OAuth authentication via JACLogin.
"""

from urllib.parse import urljoin
from sjtusuite.auth import OAuthClientBase, JACLogin


class CanvasClient(OAuthClientBase):
    """Client for interacting with SJTU Canvas LMS API."""

    def __init__(self, jac_login, session_file="canvas_client.cookies"):
        super().__init__("CanvasClient", session_file)
        self.jac_login = jac_login
        self.canvas_base = "https://oc.sjtu.edu.cn"
        self.api_base = f"{self.canvas_base}/api/v1"
        self.logged_in = False

    def validate_session(self):
        """Check if current session is valid by making a test API request."""
        if len(self.session.cookies) == 0:
            return False
        
        try:
            response = self.session.get(
                f"{self.api_base}/users/self",
                allow_redirects=False
            )
            if response.status_code == 200:
                self.logged_in = True
                return True
        except Exception as e:
            self.logger.debug("Session validation failed: %s", e)
        
        return False

    def login(self):
        """
        Perform OAuth login to Canvas via JAccount.
        
        Returns:
            bool: True on success, False on failure
        """
        if self.validate_session():
            self.logger.debug("Existing session valid, skipping login")
            return True

        self.logger.info("Starting Canvas login flow...")
        
        # Canvas returns 401 for unauthenticated root requests,
        # so we start directly from the login endpoint
        current_url = f"{self.canvas_base}/login/openid_connect"
        max_steps = 20
        steps = 0

        try:
            response = self.session.get(current_url, allow_redirects=False)
        except Exception as e:
            self.logger.error("Initial request failed: %s", e)
            return False

        while steps < max_steps:
            steps += 1

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location:
                    break
                
                if location.startswith('/'):
                    location = urljoin(current_url, location)

                self.logger.debug("Redirect %d: %s", steps, location[:80])

                if "jaccount.sjtu.edu.cn" in location:
                    self.logger.info("JAccount OAuth redirect, performing login...")
                    try:
                        final_redirect = self.jac_login.login(location)
                        self.logger.debug("JAccount returned: %s", final_redirect[:80])
                        current_url = final_redirect
                        response = self.session.get(current_url, allow_redirects=False)
                        continue
                    except Exception as e:
                        self.logger.error("JAccount login failed: %s", e)
                        return False
                
                current_url = location
                response = self.session.get(current_url, allow_redirects=False)
                continue

            if response.status_code == 200:
                # Check if we're at the Canvas dashboard
                if self.canvas_base in current_url and "/login" not in current_url:
                    self.logger.info("Canvas login successful")
                    self.logged_in = True
                    self.save_session()
                    return True
                
                # Still on login page, check for JAccount link
                if 'href="/login/openid_connect"' in response.text:
                    self.logger.info("Found Canvas login page, following JAccount link...")
                    current_url = urljoin(current_url, "/login/openid_connect")
                    response = self.session.get(current_url, allow_redirects=False)
                    continue

                # May have landed on dashboard
                if "dashboard" in current_url or "courses" in response.text.lower():
                    self.logger.info("Canvas login successful (dashboard detected)")
                    self.logged_in = True
                    self.save_session()
                    return True

                break

            # 401 at root is expected for unauthenticated, but not elsewhere
            if response.status_code == 401:
                if current_url == self.canvas_base or current_url == f"{self.canvas_base}/":
                    current_url = f"{self.canvas_base}/login/openid_connect"
                    response = self.session.get(current_url, allow_redirects=False)
                    continue

            self.logger.error("Unexpected status: %d at %s", response.status_code, current_url)
            break

        self.logger.error("Login flow terminated without success")
        return False

    def _api_request(self, endpoint, method="GET", params=None, json_data=None):
        """
        Make an authenticated API request to Canvas.
        
        Args:
            endpoint: API endpoint path (e.g., "/users/self/courses")
            method: HTTP method
            params: Query parameters
            json_data: JSON body for POST requests
            
        Returns:
            dict or list: JSON response data, or None on failure
        """
        if not self.logged_in:
            if not self.login():
                raise ValueError("Canvas login required")

        url = f"{self.api_base}{endpoint}"
        headers = {"Accept": "application/json"}

        self.logger.debug("API request: %s %s", method, url)

        if method.upper() == "GET":
            response = self.session.get(url, params=params, headers=headers)
        elif method.upper() == "POST":
            response = self.session.post(url, params=params, json=json_data, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            self.logger.warning("Session expired, re-authenticating...")
            self.logged_in = False
            if self.login():
                return self._api_request(endpoint, method, params, json_data)
            return None

        if response.status_code != 200:
            self.logger.error("API request failed: HTTP %d", response.status_code)
            return None

        try:
            return response.json()
        except Exception as e:
            self.logger.error("Failed to parse API response: %s", e)
            return None

    def get_courses(self, include_ended=False):
        """
        Get list of enrolled courses for current user.
        
        Args:
            include_ended: Include courses that have already ended
            
        Returns:
            list: List of course dictionaries with id, name, code, etc.
        """
        params = {
            "per_page": 100,
            "include[]": ["term", "total_scores"]
        }

        if not include_ended:
            params["enrollment_state"] = "active"

        courses = self._api_request("/users/self/courses", params=params)

        if courses is None:
            return []

        self.logger.info("Found %d courses", len(courses))
        return courses

    def get_course_external_tools(self, course_id):
        """
        Get external tools for a course (to find video recording tool).
        
        Args:
            course_id: Canvas course ID
            
        Returns:
            list: List of external tool dictionaries
        """
        tools = self._api_request(f"/courses/{course_id}/external_tools")
        return tools if tools else []

    def get_course_video_url(self, course_id, tool_id=8329):
        """
        Generate the external tool URL for video recording.
        
        Args:
            course_id: Canvas course ID
            tool_id: External tool ID (default: 8329 for video recording)
            
        Returns:
            str: URL to launch the video recording tool
        """
        return f"{self.canvas_base}/courses/{course_id}/external_tools/{tool_id}?display=borderless"

    def get_courses_with_video(self):
        """
        Get courses that likely have video recording enabled.
        Filters to only current/recent courses.
        
        Returns:
            list: List of course dicts with video_url added
        """
        courses = self.get_courses(include_ended=False)
        
        result = []
        for course in courses:
            course_id = course.get("id")
            if course_id:
                course["video_url"] = self.get_course_video_url(course_id)
                result.append(course)

        return result


if __name__ == "__main__":
    import logging
    from sjtusuite.core import log
    from sjtusuite.auth import get_test_jac_login

    log.DEFAULT_LOG_LEVEL = logging.INFO
    logger = log.get_logger("CanvasClientTest", level=logging.INFO)

    jac_login = get_test_jac_login()
    client = CanvasClient(jac_login)

    logger.info("Logging in to Canvas...")
    if client.login():
        logger.info("Login successful!")

        logger.info("Fetching courses...")
        courses = client.get_courses_with_video()

        for course in courses[:10]:
            logger.info("  - [%s] %s", course.get("id"), course.get("name"))
            logger.info("    Video URL: %s", course.get("video_url", "N/A"))
    else:
        logger.error("Login failed!")
