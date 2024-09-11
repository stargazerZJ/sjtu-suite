import os
from http.cookiejar import LWPCookieJar

import requests

from log import get_logger


class OAuthClientBase:
    def __init__(self, name="OAuthClientBase", session_file="oauth_client.cookies"):
        self.jac_base_url = "https://jaccount.sjtu.edu.cn"
        self.logger = get_logger(name)
        self.session = requests.Session()
        self.session.cookies = LWPCookieJar(session_file)
        self.set_default_headers()
        self.load_session()

    def set_default_headers(self):
        """Set default headers for the session."""
        default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
        }
        self.session.headers.update(default_headers)
        self.logger.debug(f"Default headers set: {default_headers}")

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
