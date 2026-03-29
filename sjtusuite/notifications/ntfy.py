"""ntfy.sh notification client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass(slots=True)
class NtfyNotificationResult:
    ok: bool
    status_code: int
    topic: str
    response_text: str
    message_id: str | None = None


@dataclass(slots=True)
class NtfyNotifier:
    topic: str
    server: str = "https://ntfy.sh"
    token: str | None = None
    default_tags: tuple[str, ...] = field(default_factory=tuple)
    default_priority: str | int | None = None
    timeout: float = 10.0
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.server = (self.server or "https://ntfy.sh").rstrip("/")
        self.topic = self.topic.strip()
        self.default_tags = tuple(tag.strip() for tag in self.default_tags if tag and tag.strip())
        if not self.topic:
            raise ValueError("ntfy topic must not be empty.")

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "NtfyNotifier | None":
        if not config or not config.get("enabled", True):
            return None
        topic = str(config.get("topic") or "").strip()
        if not topic:
            return None
        tags = config.get("tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        elif not isinstance(tags, list):
            tags = []
        return cls(
            topic=topic,
            server=str(config.get("server") or "https://ntfy.sh").strip() or "https://ntfy.sh",
            token=str(config.get("token")).strip() if config.get("token") else None,
            default_tags=tuple(str(item).strip() for item in tags if str(item).strip()),
            default_priority=config.get("priority"),
        )

    @property
    def publish_url(self) -> str:
        return f"{self.server}/{self.topic}"

    def send(
        self,
        message: str,
        *,
        title: str | None = None,
        tags: list[str] | tuple[str, ...] | str | None = None,
        priority: str | int | None = None,
        click: str | None = None,
    ) -> NtfyNotificationResult:
        merged_tags: list[str] = list(self.default_tags)
        if isinstance(tags, str):
            merged_tags.extend(item.strip() for item in tags.split(",") if item.strip())
        elif tags:
            merged_tags.extend(str(item).strip() for item in tags if str(item).strip())

        headers = {"Content-Type": "text/plain; charset=utf-8"}
        if title:
            headers["Title"] = title
        if merged_tags:
            headers["Tags"] = ",".join(dict.fromkeys(merged_tags))
        chosen_priority = priority if priority is not None else self.default_priority
        if chosen_priority is not None:
            headers["Priority"] = str(chosen_priority)
        if click:
            headers["Click"] = click
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        response = self.session.post(
            self.publish_url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()

        payload: dict[str, Any] = {}
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = response.json()

        return NtfyNotificationResult(
            ok=True,
            status_code=response.status_code,
            topic=self.topic,
            response_text=response.text,
            message_id=payload.get("id"),
        )
