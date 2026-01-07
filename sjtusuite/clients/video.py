"""
SJTU Canvas Video Client - Download course video playback from SJTU's Canvas-integrated video system.

Architecture:
1. Canvas (oc.sjtu.edu.cn) - Entry point, requires JAccount OAuth login
2. Video Portal (v.sjtu.edu.cn) - Video player UI, receives authentication via tokenId
3. Video CDN (live.sjtu.edu.cn) - Hosts MP4 files, requires temporary key parameter

API Endpoints:
- /jy-application-canvas-sjtu/directOnDemandPlay/findVodVideoList - List lecture videos
- /jy-application-canvas-sjtu/directOnDemandPlay/getVodVideoInfos - Get video sources and auth keys
"""

import os
import re
from urllib.parse import urlparse, parse_qs, urljoin, quote
from sjtusuite.auth import OAuthClientBase, JACLogin


class VideoClient(OAuthClientBase):
    """Client for downloading SJTU course video playback."""

    def __init__(self, jac_login: JACLogin, session_file="video_client.cookies"):
        super().__init__("VideoClient", session_file)
        self.jac_login = jac_login
        self.canvas_base = "https://oc.sjtu.edu.cn"
        self.video_base = "https://v.sjtu.edu.cn"
        self.api_base = f"{self.video_base}/jy-application-canvas-sjtu"
        self.token_id = None
        self.token_id = None
        self.token = None  # JWT-like API token from headers
        self.canvas_course_id = None # Extracted courId needed for requests

    def validate_session(self):
        """Check if current session is valid."""
        if len(self.session.cookies) > 0 and self.token_id:
            self.logger.debug("Session cookies and tokenId exist, might be valid.")
            return True
        self.logger.debug("No valid session.")
        return False



    def login(self, course_url):
        """
        Perform OAuth login via Canvas and extract tokenId from video portal.
        
        Args:
            course_url: Canvas external tool URL.
        
        Returns:
            str: The tokenId for video portal access, or None on failure
        """
        self.logger.info("Starting login flow from Canvas URL: %s", course_url)
        
        current_url = course_url
        max_steps = 25
        steps = 0
        response = None

        # Initial request
        try:
            response = self.session.get(current_url, allow_redirects=False)
        except Exception as e:
            self.logger.error("Initial request failed: %s", e)
            return None
        
        while steps < max_steps:
            steps += 1
            
            # Check for tokenId in URL (e.g. from a redirect or initial URL)
            if "tokenId=" in current_url:
                token_match = re.search(r'tokenId=([^&\s#]+)', current_url)
                if token_match:
                    self.token_id = token_match.group(1)
                    self.logger.info("Extracted tokenId from URL: %s...", self.token_id[:20])
                    self._exchange_token_id()
                    self.save_session()
                    return self.token_id

            # 1. Handle Redirects (3xx)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if not location:
                    break
                
                if location.startswith('/'):
                    location = urljoin(current_url, location)
                
                self.logger.debug("Redirect %d: %s", steps, location)
                
                # Check for JAccount OAuth
                if "jaccount.sjtu.edu.cn" in location:
                    self.logger.info("JAccount OAuth redirect detected, performing login...")
                    try:
                        final_redirect = self.jac_login.login(location)
                        self.logger.info("OAuth login completed, continuing from: %s", final_redirect)
                        current_url = final_redirect
                        response = self.session.get(current_url, allow_redirects=False)
                        continue
                    except Exception as e:
                        self.logger.error("JAccount login failed: %s", e)
                        return None
                    
                current_url = location
                response = self.session.get(current_url, allow_redirects=False)
                continue
            
            # 2. Handle OK (200) - Check content for Token, Login Link, or LTI Form
            if response.status_code == 200:
                content = response.text
                
                # Check for tokenId in content
                # Search both generic and specific patterns
                token_patterns = [
                    r'tokenId["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                    r'"tokenId"\s*:\s*"([^"]+)"',
                ]
                for pattern in token_patterns:
                    match = re.search(pattern, content)
                    if match:
                        self.token_id = match.group(1)
                        self.logger.info("Extracted tokenId from page content: %s...", self.token_id[:20])
                        self._exchange_token_id()
                        self.save_session()
                        return self.token_id
                
                # Check for Canvas login page with JAccount button
                if 'href="/login/openid_connect"' in content or 'id="jaccount"' in content:
                    self.logger.info("Found Canvas login page with JAccount button, following...")
                    current_url = urljoin(current_url, "/login/openid_connect")
                    response = self.session.get(current_url, allow_redirects=False)
                    continue

                # Check for LTI form that posts to video portal
                # Can match v.sjtu.edu.cn in action
                form_match = re.search(r'action="([^"]*v\.sjtu\.edu\.cn[^"]*)"', content)
                if form_match:
                    self.logger.debug("Found LTI form, following form action...")
                    form_fields = {}
                    for field_match in re.finditer(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', content):
                        form_fields[field_match.group(1)] = field_match.group(2)
                    
                    if form_fields:
                        self.logger.debug("LTI Form Fields: %s", list(form_fields.keys()))
                        form_action = form_match.group(1)
                        if form_action.startswith('/'):
                            form_action = urljoin(current_url, form_action)
                        
                        self.logger.debug("Submitting LTI form to: %s", form_action)
                        response = self.session.post(form_action, data=form_fields, allow_redirects=False)
                        # We use the submit result as the 'response' for the next iteration (checking for redirect or token)
                        current_url = form_action # The URL associated with the response is technically the action
                        continue
                
                # If we are here, we might be at the video portal but failed to parse everything
                if "v.sjtu.edu.cn" in current_url:
                     self.logger.warning("At video portal but no tokenId found/extracted.")
                     # Maybe we need to log content to debug
                     # self.logger.debug("Content: %s", content[:500])
                     break
                
                self.logger.debug("Stuck at page: %s", current_url)
                break
            
            # Non-200, Non-3xx
            self.logger.error("Unexpected status code: %d at %s", response.status_code, current_url)
            break
        
        self.logger.error("Login flow terminated without tokenId")
        return None

    def _exchange_token_id(self):
        """Exchange tokenId for JWT token via /lti3/getAccessTokenByTokenId."""
        if not self.token_id:
            return False
            
        endpoint = "/lti3/getAccessTokenByTokenId"
        url = f"{self.api_base}{endpoint}"
        
        try:
            self.logger.info("Exchanging tokenId for API token...")
            response = self.session.get(url, params={"tokenId": self.token_id})
            
            if response.status_code == 200:
                data = response.json()
                if str(data.get("code")) == "0" or data.get("success"):
                     token_data = data.get("data", {})
                     self.token = token_data.get("token")
                     params = token_data.get("params", {})
                     self.canvas_course_id = params.get("courId")
                     
                     if self.token:
                         self.logger.info("Token exchange successful! (courId: %s)", 
                                        self.canvas_course_id[:10] if self.canvas_course_id else "None")
                         self.save_session()
                         return True
                     else:
                         self.logger.error("Token field not found in exchange data")
                else:
                    self.logger.error("Token exchange API error: %s", data.get("message"))
            else:
                self.logger.error("Token exchange HTTP error: %d", response.status_code)
                
        except Exception as e:
            self.logger.error("Token exchange exception: %s", e)
            
        return False

    def _get_api_token(self):
        """
        Get API token for authenticated API requests.
        """
        if self.token:
            return self.token
            
        if self.token_id:
            if self._exchange_token_id():
                return self.token

        raise ValueError("No valid API token available, call login() first")

    def _api_request(self, endpoint, method="GET", params=None, json_data=None, files=None):
        """
        Make an authenticated API request to the video portal.
        
        Args:
            endpoint: API endpoint path (without base URL)
            method: HTTP method
            params: Query parameters
            json_data: JSON body for POST requests
            files: Dictionary for multipart/form-data requests.
            
        Returns:
            dict: JSON response data, or None on failure
        """
        url = f"{self.api_base}{endpoint}"
        headers = {
            "token": self._get_api_token(),
            "Accept": "application/json",
            "Content-Type": "application/json" if json_data else None
        }
        # Remove None headers
        headers = {k: v for k, v in headers.items() if v is not None}
        
        self.logger.debug("API request: %s %s", method, url)
        
        if method.upper() == "GET":
            response = self.session.get(url, params=params, headers=headers)
        elif method.upper() == "POST":
            response = self.session.post(url, params=params, json=json_data, files=files, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        if response.status_code != 200:
            self.logger.error("API request failed: HTTP %d", response.status_code)
            return None
        
        try:
            data = response.json()
            if data.get("code") == 0 or data.get("success"):
                return data.get("data", data)
            else:
                self.logger.error("API error: %s", data.get("message", "Unknown error"))
                return None
        except Exception as e:
            self.logger.error("Failed to parse API response: %s", e)
            return None

    def get_sessions(self, lti_course_id=None):
        """
        Fetch list of video sessions (lectures) for the course.
        
        Args:
            lti_course_id: Optional LTI course ID. If not provided, will try to extract from tokenId.
            
        Returns:
            list: List of session dictionaries with video metadata
        """
        # The findVodVideoList endpoint lists all lectures
        endpoint = "/directOnDemandPlay/findVodVideoList"
        
        params = {}
        if lti_course_id:
            params["ltiCourseId"] = lti_course_id
            
        if self.canvas_course_id:
            # Verified: this field must be URL-encoded for the backend
            params["canvasCourseId"] = quote(self.canvas_course_id)
        
        # API requires POST for this endpoint
        data = self._api_request(endpoint, method="POST", json_data=params)
        
        if data is None:
            self.logger.error("Failed to fetch video sessions")
            return []
        
        sessions = data if isinstance(data, list) else data.get("records", [])
        self.logger.info("Found %d video sessions", len(sessions))
        return sessions

    def get_video_info(self, video_id=None, session_info=None):
        """
        Get detailed video information including streaming URLs with auth keys.
        
        Args:
            video_id: The videoId from session info
            session_info: Full session info dict (alternative to video_id)
            
        Returns:
            dict: Video info with rtmpUrlHdv (MP4 URL) and other metadata
        """
        if session_info and not video_id:
            video_id = session_info.get("videoId") or session_info.get("id")
        
        if not video_id:
            raise ValueError("video_id or session_info with videoId required")
        
        endpoint = "/directOnDemandPlay/getVodVideoInfos"
        
        # API requires multipart/form-data
        # We use 'files' for multipart in requests, even for text fields
        multipart_data = {
            "playTypeHls": (None, "true"),
            "isAudit": (None, "true"),
            "id": (None, video_id)
        }
        
        data = self._api_request(endpoint, method="POST", files=multipart_data)
        
        if data is None:
            self.logger.error("Failed to fetch video info for videoId: %s", video_id)
            return None
        
        self.logger.debug("Video info: %s", data.get("videName", "Unknown"))
        return data

    def get_video_urls(self, video_info, channels=None):
        """
        Extract video URLs from video info response.
        
        Args:
            video_info: Response from get_video_info()
            channels: List of channel indices to return (default: all available)
            
        Returns:
            list: List of (channel_index, url, description) tuples
        """
        urls = []
        
        # Main video URL
        main_url = video_info.get("rtmpUrlHdv")
        if main_url:
            urls.append((0, main_url, "Main camera"))
        
        # Additional camera angles
        video_list = video_info.get("videoPlayResponseVoList", [])
        for i, cam in enumerate(video_list):
            url = cam.get("rtmpUrlHdv")
            if url:
                desc = cam.get("name", f"Camera {i+1}")
                urls.append((i+1, url, desc))
        
        # Filter by channels if specified
        if channels:
            urls = [u for u in urls if u[0] in channels]
        
        return urls

    def download_video(self, url, output_path, progress_callback=None):
        """
        Download a video file from the given URL.
        
        Args:
            url: Direct MP4 URL with auth key
            output_path: Local file path to save the video
            progress_callback: Optional callback(downloaded_bytes, total_bytes)
            
        Returns:
            bool: True on success, False on failure
        """
        self.logger.info("Downloading video to: %s", output_path)
        
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://v.sjtu.edu.cn/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        }
        
        try:
            # Stream the download with proper headers
            response = self.session.get(url, stream=True, headers=headers)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            # Create directory if needed
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total_size)
            
            self.logger.info("Download complete: %s (%.2f MB)", 
                           output_path, downloaded / (1024 * 1024))
            return True
            
        except Exception as e:
            self.logger.error("Download failed: %s", e)
            return False

    def list_sessions(self, course_url):
        """
        Convenience method: login and list all video sessions for a course.
        
        Args:
            course_url: Canvas external tool URL
            
        Returns:
            list: List of session dictionaries
        """
        if not self.token_id:
            self.login(course_url)
        
        return self.get_sessions()

    def download_session(self, session_info, output_dir="./videos", channels=None):
        """
        Download all videos for a session.
        
        Args:
            session_info: Session dict from get_sessions()
            output_dir: Directory to save videos
            channels: List of channel indices to download (default: all)
            
        Returns:
            list: List of (output_path, success) tuples
        """
        video_info = self.get_video_info(session_info=session_info)
        if not video_info:
            return []
        
        urls = self.get_video_urls(video_info, channels)
        results = []
        
        video_name = video_info.get("videName", "video").replace("/", "-")
        
        for channel, url, desc in urls:
            filename = f"{video_name}_ch{channel}.mp4"
            output_path = os.path.join(output_dir, filename)
            
            # Simple progress reporter
            def progress(downloaded, total):
                if total > 0:
                    pct = downloaded * 100 / total
                    print(f"\r  Downloading {desc}: {pct:.1f}%", end="", flush=True)
            
            success = self.download_video(url, output_path, progress_callback=progress)
            print()  # Newline after progress
            results.append((output_path, success))
        
        return results


if __name__ == "__main__":
    import logging
    from sjtusuite.core import log
    from sjtusuite.auth import get_test_jac_login

    log.DEFAULT_LOG_LEVEL = logging.INFO
    logger = log.get_logger("VideoClientTest", level=logging.INFO)

    jac_login = get_test_jac_login()
    client = VideoClient(jac_login)

    # Test URL, this URL is passed in by other clients that calls VideoClient
    # Replace it if you want to test another course
    course_id = 81737
    test_course_url = f"https://oc.sjtu.edu.cn/courses/{course_id}/external_tools/8329?display=borderless"
    
    logger.info("Testing minimal login flow...")
    ticket = client.login(test_course_url)
    
    if ticket and client.token:
        logger.info("Login successful. Token: %s...", client.token[:20])
        
        # List sessions
        logger.info("Fetching video sessions...")
        sessions = client.get_sessions()
        
        if sessions:
            logger.info("Found %d sessions", len(sessions))
            # Just show first session info as example
            first_session = sessions[0]
            logger.info("First session: %s", first_session.get("videoName", "Unknown"))
            
            # Get video info
            logger.info("Getting video info...")
            video_info = client.get_video_info(session_info=first_session)
            
            if video_info:
                urls = client.get_video_urls(video_info)
                for ch, url, desc in urls:
                    logger.info("Stream %d (%s): %s", ch, desc, url)
        else:
            logger.warning("No sessions found")
    else:
        logger.error("Login failed or token exchange failed.")
