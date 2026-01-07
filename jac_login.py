import requests
from urllib.parse import urlparse, parse_qs
import re
import time
import io
from log import get_logger
from oauth_client import OAuthClientBase


def extract_auth_params(auth_url):
    """Extract params from the authorization URL."""
    parsed_url = urlparse(auth_url)
    params = parse_qs(parsed_url.query)
    return params


class JACLogin(OAuthClientBase):
    def __init__(self, username, password, session_file="jac_login.cookies"):
        self.username = username
        self.password = password
        self.login_base_url = "https://jaccount.sjtu.edu.cn"
        super().__init__("JACLogin", session_file)

    def login(self, auth_url: str):
        """
        Perform the JAccount OAuth2 login flow.
        The caller should pass the redirect location url to this method
        once the url belongs to domain jaccount.sjtu.edu.cn .
        The caller should not access the redirect location url directly.

        :param auth_url: The authorization URL obtained from the initial request.
        :return: The final redirect URL after successful login.
        """
        # The OAuth2 redirect flow:
        # /oauth2/authorize -> /jaccount/jalogin -> (post to /jaccount/ulogin and refresh if not logged in)
        # -> /oauth2/authorize -> final redirect url

        # Make sure the authorization URL is valid
        if not auth_url.startswith(self.login_base_url + "/oauth2/authorize"):
            raise ValueError("Invalid authorization URL")

        # Navigate to login page
        login_url = self.session.get(auth_url, allow_redirects=False).headers["Location"]

        auth_url = self.do_login(login_url)

        final_redirect_response = self.session.get(auth_url, allow_redirects=False)
        final_redirect_url = final_redirect_response.headers["Location"]
        self.save_session()
        self.logger.debug(f"Logged in as {self.username}.")
        return final_redirect_url

    def handle_2fa(self, login_page):
        self.logger.info("Two-Step Verification required.")
        
        match = re.search(r"account:\s*'([^']+)'", login_page.text)
        if match:
            account = match.group(1)
        else:
            self.logger.warning("Could not extract account from 2FA page, using configured username.")
            account = self.username

        print("Select 2FA method:")
        print("1. My SJTU App (app)")
        print("2. Email (email)")
        print("3. SMS (sms)")
        choice = input("Enter choice (1/2/3 or name): ").strip().lower()

        method = "app"
        if choice in ["2", "email"]:
            method = "email"
        elif choice in ["3", "sms"]:
            method = "sms"

        self.logger.info(f"Sending code via {method}...")
        r = self.session.post(
            "https://jaccount.sjtu.edu.cn/jaccount/2fa/loginVerify",
            data={"c": method},
            headers={"X-Requested-With": "XMLHttpRequest"}
        )

        if r.json().get("errno") != 0:
            self.logger.error(f"Failed to send code: {r.json().get('error')}")
            return False

        code = input("Enter the code you received: ")

        self.logger.info("Submitting code...")
        r = self.session.post(
            "https://jaccount.sjtu.edu.cn/jaccount/2faVerify",
            data={
                "account": account,
                "captcha": code,
                "trust": "true"
            },
            headers={"X-Requested-With": "XMLHttpRequest"}
        )

        if r.json().get("errno") == 0:
            self.logger.info("2FA successful.")
            return True
        else:
            self.logger.error(f"2FA failed: {r.json().get('error')}")
            return False

    def do_login(self, login_url, retry_count=3):
        """Perform the login process."""

        for i in range(retry_count + 1):
            login_page = self.session.get(login_url, allow_redirects=False)
            if login_page.status_code == 302 and login_page.headers[
                "Location"
            ].startswith(self.login_base_url + "/oauth2/authorize"):
                # login is successful
                if i > 0:
                    self.logger.info(f"Login session established or refreshed, user: {self.username}")
                return login_page.headers["Location"]
            self.logger.debug(f"Login attempt {i + 1}/{retry_count}")
            params = extract_auth_params(login_page.url)
            match = re.search(r'uuid: "([0-9a-f-]+)"', login_page.text)
            if not match:
                if "Two-Step Verification" in login_page.text:
                    if self.handle_2fa(login_page):
                        continue
                    else:
                        raise ValueError("2FA failed.")
            uuid = match.group(1)
            captcha = self.get_captcha(uuid, login_page.url)
            captcha = self.solve_captcha(captcha)
            time.sleep(0.5 if i == 0 else 1.0)
            response = self.session.post(
                "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
                data={
                    "user": self.username,
                    "pass": self.password,
                    "uuid": uuid,
                    "captcha": captcha,
                    **params,
                },
                headers={"accept-language": "zh-CN"},
                allow_redirects=False,
            )
            # if the login is successful or happens too quickly, the response will be html. Otherwise, it will be json.
            # e.g. {"errno":1,"error":"请正确填写验证码","code":"WRONG_CAPTCHA","url":null}
            if response.headers.get("Content-Type", "") == "application/json" and response.json().get("errno") == 1:
                self.logger.warning(f"Login error: {response.json()['error']}")
                if response.json()["code"] == "WRONG_USER_OR_PASSWORD":
                    raise ValueError("Invalid username or password.")
        raise ValueError(f"Failed to login after {retry_count} attempts.")

    def get_captcha(self, uuid, referer):
        image = self.session.get(
            "https://jaccount.sjtu.edu.cn/jaccount/captcha",
            params={"uuid": uuid,
                    "t": time.time_ns() // 1000000},
            headers={"Referer": referer},
        )
        return image.content

    def solve_captcha(self, image):
        try:
            self.logger.info("Solving captcha")
            r = requests.post(
                "https://geek.sjtu.edu.cn/captcha-solver/",
                files={"image": ("captcha.jpg", io.BytesIO(image))}
            )
            return r.json()["result"]
        except Exception as e:
            with open("captcha.jpg", "wb") as f:
                f.write(image)
            return input("Please solve the captcha and enter the result: ")

def get_test_jac_login():
    with open("password.txt") as f:
        username = f.readline().strip()
        password = f.readline().strip()
    return JACLogin(username, password)

if __name__ == "__main__":
    import sys
    import log
    import logging
    from urllib.parse import urljoin

    # Configure the root logger for demo purposes
    log.DEFAULT_LOG_LEVEL = logging.DEBUG

    # # Example usage
    # if len(sys.argv) != 2:
    #     print("Usage: python jac_login.py <username>")
    #     sys.exit(1)

    # username = sys.argv[1]
    # password = getpass.getpass("Enter your password: ")

    logger = get_logger("LoginTest")

    jac_login = get_test_jac_login()
    session = requests.Session()
    try:
        current_url = "https://my.sjtu.edu.cn"
        # current_url = "https://i.sjtu.edu.cn/jaccountlogin"
        logger.info(f"Starting login process from URL: {current_url}")
        while not current_url.startswith("https://jaccount.sjtu.edu.cn"):
            response = session.get(current_url, allow_redirects=False)
            if "Location" in response.headers:
                redirect_url = response.headers["Location"]
                if redirect_url.startswith('/'):
                    # Handle relative URLs by combining with the base url
                    current_url = urljoin(current_url, redirect_url)
                else:
                    current_url = redirect_url
                # Logging here can help with debugging and understanding redirection flow.
                logger.debug(f"Redirecting to: {current_url}")
            else:
                # Break the loop if there is no redirect location, to prevent an infinite loop.
                logger.error("No redirect location found in headers.")
                raise ValueError("Failed to follow redirects to JAccount login page.")

        redirect_url = jac_login.login(current_url)
        print(f"Logged in successfully! Redirect URL: {redirect_url}")
    except Exception as e:
        logger.error(f"An error occurred during login: {e}", exc_info=True)
        sys.exit(1)
