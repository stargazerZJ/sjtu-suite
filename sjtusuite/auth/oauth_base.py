import os
from http.cookiejar import LWPCookieJar

import requests

from sjtusuite.core.log import get_logger


class OAuthClientBase:
    def __init__(self, name="OAuthClientBase", session_file="oauth_client.cookies"):
        self.jac_base_url = "https://jaccount.sjtu.edu.cn"
        self.logger = get_logger(name)
        self.session = requests.Session()
        self.session.cookies = LWPCookieJar(session_file)
        self.set_default_headers()
        self.load_session()

    def set_user_agent(self, agent_type="chrome"):
        """Set custom User-Agent for the session.
        
        Args:
            agent_type: "chrome" for desktop Chrome, "mobile" for iPhone TaskCenter App
        """
        ua_map = {
            "chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148; TaskCenterApp/3.4.5/iPhone 13/ScreenFringe (iOS,iPhone,18.1.1; Scale/3.0)"
        }
        if agent_type not in ua_map:
            self.logger.warning(f"Unknown agent_type: {agent_type}, using chrome UA")
            agent_type = "chrome"
        
        self.session.headers.update({'User-Agent': ua_map[agent_type]})
        self.logger.debug(f"User-Agent set to: {ua_map[agent_type]}")

    def set_default_headers(self):
        """Set default headers for the session."""
        self.set_user_agent("chrome")

    def save_session(self):
        """Save session cookies to disk."""
        self.logger.debug("Saving session cookies to disk.")
        self.session.cookies.save(ignore_discard=True)

    def load_session(self):
        """Load session cookies from disk."""
        if os.path.exists(self.session.cookies.filename):
            self.session.cookies.load(ignore_discard=True)
            self.logger.debug("Session cookies loaded from disk.")
        else:
            self.logger.info("No session cookies found on disk.")
