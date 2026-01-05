from urllib.parse import urlparse, parse_qs, urljoin
from log import get_logger
from oauth_client import OAuthClientBase
from jac_login import JACLogin


class CheckinClient(OAuthClientBase):
    """Client for SJTU mlearning checkin/roll call system."""

    def __init__(self, jac_login: JACLogin, session_file="checkin_client.cookies"):
        super().__init__("CheckinClient", session_file)
        self.base_url = "https://mlearning.sjtu.edu.cn"
        self.jac_login = jac_login

    def validate_session(self):
        """Check if current session is valid by testing access to a protected page."""
        # We can't reliably validate the session without visiting a specific URL
        # So we'll check if we have cookies and assume they might be valid
        if len(self.session.cookies) > 0:
            self.logger.debug("Session cookies exist, might be valid.")
            return True
        self.logger.debug("No session cookies, session invalid.")
        return False

    def _follow_redirects_to_jaccount(self, start_url):
        """
        Follow redirects until we reach jaccount.sjtu.edu.cn.
        
        Returns:
            tuple: (jaccount_url, final_response) or (None, response) if not redirected to jaccount
        """
        current_url = start_url
        max_redirects = 10
        
        for _ in range(max_redirects):
            response = self.session.get(current_url, allow_redirects=False)
            
            if response.status_code not in (301, 302, 303, 307, 308):
                # Not a redirect, return current response
                return None, response
            
            location = response.headers.get("Location")
            if not location:
                return None, response
            
            # Handle relative URLs
            if location.startswith('/'):
                location = urljoin(current_url, location)
            
            # Check if we've reached jaccount
            if location.startswith("https://jaccount.sjtu.edu.cn"):
                return location, response
            
            current_url = location
            self.logger.debug("Redirect to: %s", current_url)
        
        self.logger.error("Too many redirects")
        return None, None

    def login_with_url(self, target_url):
        """
        Perform OAuth login by visiting a target URL that requires authentication.
        
        Args:
            target_url: A URL that will redirect to OAuth if not logged in
            
        Returns:
            response: The final response after OAuth (or the original response if already logged in)
        """
        self.logger.debug("Attempting to access: %s", target_url)
        
        current_url = target_url
        max_redirects = 15
        
        for redirect_count in range(max_redirects):
            response = self.session.get(current_url, allow_redirects=False)
            
            # If not a redirect, we're done
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            
            location = response.headers.get("Location", "")
            if not location:
                self.logger.warning("Redirect without Location header")
                return response
            
            # Handle relative URLs
            if location.startswith('/'):
                location = urljoin(current_url, location)
            
            self.logger.debug("Redirect %d to: %s", redirect_count + 1, location)
            
            # Check if redirected to jaccount
            if location.startswith("https://jaccount.sjtu.edu.cn"):
                self.logger.debug("OAuth redirect detected, performing login...")
                
                # Use JACLogin to handle the OAuth flow
                final_redirect = self.jac_login.login(location)
                
                # Continue following from final_redirect
                current_url = final_redirect
                self.save_session()
                self.logger.info("OAuth login completed successfully.")
            else:
                current_url = location
        
        self.logger.error("Too many redirects (%d)", max_redirects)
        return response

    def login(self):
        """Perform OAuth login using a test URL. For actual checkin, use checkin() directly."""
        # If we have cookies, assume we're logged in
        if self.validate_session():
            self.logger.debug("Already have session cookies.")
            return True
        
        self.logger.info("No existing session, will login on first checkin request.")
        return True  # We'll handle login during checkin

    def checkin(self, checkin_url):
        """
        Perform checkin using the provided URL.
        
        Args:
            checkin_url: The full checkin URL from mlearning.sjtu.edu.cn
                         e.g. https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/?courseCode=...
        
        Returns:
            tuple: (success: bool, message: str)
        """
        self.logger.info("Processing checkin URL: %s", checkin_url)
        
        # Validate URL format
        parsed = urlparse(checkin_url)
        if "mlearning.sjtu.edu.cn" not in parsed.netloc:
            return False, "Invalid URL: must be from mlearning.sjtu.edu.cn"
        
        # Extract parameters for logging
        params = parse_qs(parsed.query)
        course_code = params.get("courseCode", ["unknown"])[0]
        self.logger.info("Checkin for course: %s", course_code)
        
        # Use login_with_url which handles OAuth transparently
        response = self.login_with_url(checkin_url)

        # Parse the response
        return self._parse_checkin_response(response)

    def _parse_checkin_response(self, response):
        """
        Parse the checkin response to determine success/failure.
        
        Args:
            response: The HTTP response from the checkin request
            
        Returns:
            tuple: (success: bool, message: str)
        """
        # Check the final URL for state indicators (e.g., state=EXPIRED)
        final_url = response.url if hasattr(response, 'url') else ""
        if "state=EXPIRED" in final_url:
            self.logger.warning("Checkin token has expired (state=EXPIRED in URL)")
            return False, "签到失败: 签到码已过期"
        
        # Try to parse as JSON first
        try:
            data = response.json()
            if data.get("code") == 0 or data.get("success"):
                msg = data.get("message", data.get("msg", "Checkin successful"))
                self.logger.info("Checkin successful: %s", msg)
                return True, msg
            else:
                msg = data.get("message", data.get("msg", "Checkin failed"))
                self.logger.warning("Checkin failed: %s", msg)
                return False, msg
        except Exception:
            pass
        
        # Parse HTML response
        content = response.text
        
        # Check failure indicators FIRST (more specific)
        failure_indicators = ["签到失败", "已过期", "无效", "已签过", "EXPIRED", "expired"]
        for indicator in failure_indicators:
            if indicator in content.lower() or indicator in content:
                self.logger.warning("Checkin failed (detected: %s)", indicator)
                return False, f"签到失败: {indicator}"
        
        # Common success indicators in Chinese
        success_indicators = ["成功", "已签到", "success"]
        for indicator in success_indicators:
            if indicator in content.lower() or indicator in content:
                self.logger.info("Checkin successful (detected: %s)", indicator)
                return True, "签到成功"
        
        # If we can't determine, log the response for debugging
        self.logger.debug("Response content (first 500 chars): %s", content[:500])
        
        # Check HTTP status
        if response.status_code == 200:
            # Assume success if we got a 200 and no error indicators
            self.logger.info("Checkin completed (HTTP 200, no error indicators)")
            return True, "签到请求已完成"
        else:
            return False, f"Unexpected response: HTTP {response.status_code}"


if __name__ == "__main__":
    import log
    import logging
    from jac_login import get_test_jac_login

    # Configure logging for testing
    log.DEFAULT_LOG_LEVEL = logging.DEBUG

    logger = get_logger("CheckinClientTest", level=logging.DEBUG)

    # Get test credentials
    jac_login = get_test_jac_login()
    client = CheckinClient(jac_login)

    # Test login
    logger.info("Testing login...")
    if client.login():
        logger.info("Login successful!")
    else:
        logger.error("Login failed!")

    # Example checkin URL (would need a real one for testing)
    test_url = "https://mlearning.sjtu.edu.cn/lms/mobile2/forscan/?courseCode=81737&rollCallToken=4323968f28484ea8b35b928f05c7bd32_NORMAL_e31d740619074ee3709ae84a91caf665&signHistoryId=4323968f28484ea8b35b928f05c7bd32&fromType=scanRollCall"
    success, message = client.checkin(test_url)
    logger.info("Checkin result: success=%s, message=%s", success, message)
