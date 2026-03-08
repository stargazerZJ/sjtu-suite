import json
import os
from typing import Any, Optional
from venv import logger
from .config import get_project_root
from .log import get_logger

class CredentialProvider:
    """
    Centralized credential provider for SJTU Suite.
    Handles loading credentials from JSON file and resolving environment variables.
    """
    def __init__(self, config_path: str = "credentials.json"):
        self.logger = get_logger("CredentialProvider")
        self.config_path = get_project_root() / config_path
        self._data = self._load_data()

    def _load_data(self) -> dict:
        """Load data from credentials file if it exists."""
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    def _resolve_value(self, value: Any) -> Any:
        """Resolve value, handling environment variable references."""
        if isinstance(value, dict) and value.get("type") == "env":
            env_key = value.get("key")
            if env_key:
                return os.environ.get(env_key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """Get a credential value by key."""
        val = self._data.get(key, default)
        return self._resolve_value(val)

    @property
    def username(self) -> Optional[str]:
        """Get JAccount username."""
        return self.get("username")

    @property
    def password(self) -> Optional[str]:
        """Get JAccount password."""
        password_block = self.get("password")
        # Warn if password uses the old format (direct string)
        if isinstance(password_block, str):
            self.logger.warning(
                "Storing password as plain string is deprecated. "
                "See README.md for new format, preferably using environment variables."
            )
            return password_block
        if isinstance(password_block, dict):
            if password_block.get("mode") == "literal":
                return password_block.get("value")
            elif password_block.get("mode") == "env":
                env_key = password_block.get("key")
                if env_key:
                    return os.environ.get(env_key)
        self.logger.warning("Password not found or improperly configured.")
        return None

    @property
    def room_id(self) -> Optional[str]:
        """Get Room ID for Door client."""
        return self.get("room_id")

    @property
    def door_access_token(self) -> Optional[str]:
        """Get Access Token for Door server."""
        return self.get("door_access_token")

    @property
    def checkin_poll_url(self) -> str:
        """Get poll URL for Checkin server."""
        return self.get("checkin_poll_url", "http://localhost:8080/checkin-data")
    
    @property
    def checkin_poll_interval(self) -> float:
        """Get poll interval for Checkin server."""
        return self.get("checkin_poll_interval", 1.0)

# Singleton instance
credentials = CredentialProvider()
