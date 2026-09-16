import json
import os
from typing import Any, Optional
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

    def _resolve_secret_block(self, value: Any, *, label: str) -> Optional[str]:
        """Resolve a secret block that may be literal or environment-backed."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if value.get("mode") == "literal":
                if value.get("value"):
                    return value.get("value")
                if value.get("key"):
                    self.logger.warning(
                        "%s uses 'key' with mode='literal'. "
                        "Treating it as a legacy literal value; rename it to 'value'.",
                        label,
                    )
                    return value.get("key")
            elif value.get("mode") == "env":
                env_key = value.get("key")
                if env_key:
                    return os.environ.get(env_key)
        return None

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
        if isinstance(password_block, str):
            self.logger.warning(
                "Storing password as plain string is deprecated. "
                "See README.md for new format, preferably using environment variables."
            )
            return password_block
        password = self._resolve_secret_block(password_block, label="Password block")
        if password:
            return password
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

    @property
    def ntfy_config(self) -> dict[str, Any]:
        """Get ntfy notification settings for daemon reuse."""
        notifications = self.get("notifications", {})
        if not isinstance(notifications, dict):
            return {}
        ntfy = notifications.get("ntfy", {})
        if not isinstance(ntfy, dict):
            return {}

        raw_tags = self._resolve_value(ntfy.get("tags", []))
        if isinstance(raw_tags, str):
            tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
        elif isinstance(raw_tags, list):
            tags = [str(item).strip() for item in raw_tags if str(item).strip()]
        else:
            tags = []

        return {
            "enabled": bool(ntfy.get("enabled", True)),
            "server": str(self._resolve_value(ntfy.get("server", "https://ntfy.sh")) or "https://ntfy.sh").strip(),
            "topic": str(self._resolve_value(ntfy.get("topic", "")) or "").strip(),
            "token": self._resolve_secret_block(ntfy.get("token"), label="Ntfy token"),
            "tags": tags,
            "priority": self._resolve_value(ntfy.get("priority")),
        }

    @property
    def mail_config(self) -> dict[str, Any]:
        """Get mail (SMTP) notification settings for daemon reuse.

        Credentials default to the shared JAccount username/password when the
        block does not carry its own `username` / `password`.
        """
        notifications = self.get("notifications", {})
        if not isinstance(notifications, dict):
            return {}
        mail = notifications.get("mail", {})
        if not isinstance(mail, dict):
            return {}

        raw_to = self._resolve_value(mail.get("to", mail.get("recipients", [])))
        if isinstance(raw_to, str):
            recipients = [item.strip() for item in raw_to.split(",") if item.strip()]
        elif isinstance(raw_to, list):
            recipients = [str(item).strip() for item in raw_to if str(item).strip()]
        else:
            recipients = []

        return {
            "enabled": bool(mail.get("enabled", True)),
            "to": recipients,
            "from": self._resolve_value(mail.get("from")),
            "subject_prefix": self._resolve_value(mail.get("subject_prefix")),
            "host": str(self._resolve_value(mail.get("host", "mail.sjtu.edu.cn")) or "mail.sjtu.edu.cn"),
            "username": self._resolve_value(mail.get("username")),
            "password": self._resolve_secret_block(mail.get("password"), label="Mail password"),
        }

# Singleton instance
credentials = CredentialProvider()
