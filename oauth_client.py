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
        self.load_session()

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
