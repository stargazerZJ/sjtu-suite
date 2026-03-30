"""Higher-level notification helpers for daemon events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .ntfy import NtfyNotifier


SEVERITY_PRIORITY = {
    "info": "default",
    "success": "default",
    "warning": "high",
    "error": "urgent",
}


@dataclass(slots=True)
class DaemonNotificationClient:
    """Best-effort wrapper around ntfy for daemon lifecycle events."""

    component: str
    notifier: NtfyNotifier | None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("daemon_notifications"))

    @classmethod
    def from_config(
        cls,
        component: str,
        config: dict[str, Any] | None,
        *,
        logger: logging.Logger | None = None,
    ) -> "DaemonNotificationClient":
        return cls(
            component=component,
            notifier=NtfyNotifier.from_config(config),
            logger=logger or logging.getLogger(component),
        )

    def notify(
        self,
        event: str,
        message: str,
        *,
        title: str | None = None,
        severity: str = "info",
        tags: list[str] | tuple[str, ...] | None = None,
        click: str | None = None,
    ) -> dict[str, Any]:
        """Send a daemon event notification without interrupting the caller."""
        if not self.notifier:
            return {"configured": False, "sent": False}

        resolved_tags = [self.component, event]
        if tags:
            resolved_tags.extend(str(tag).strip() for tag in tags if str(tag).strip())

        try:
            result = self.notifier.send(
                message,
                title=title or f"SJTU {self.component}: {event.replace('_', ' ')}",
                tags=resolved_tags,
                priority=SEVERITY_PRIORITY.get(severity, severity),
                click=click,
            )
        except Exception as exc:  # pragma: no cover - network failure path
            self.logger.warning(
                "Failed to send %s notification for %s: %s",
                self.component,
                event,
                exc,
            )
            return {
                "configured": True,
                "sent": False,
                "event": event,
                "error": str(exc),
            }

        payload = {
            "configured": True,
            "sent": True,
            "event": event,
            "topic": result.topic,
            "status_code": result.status_code,
        }
        if result.message_id:
            payload["message_id"] = result.message_id
        return payload
