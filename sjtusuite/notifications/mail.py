"""Email (SMTP) notifier for daemon and operational push notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from sjtusuite.clients.mail import MailClient, MailError


@dataclass(slots=True)
class MailNotificationResult:
    ok: bool
    from_addr: str
    recipients: str
    error: str | None = None


@dataclass(slots=True)
class MailNotifier:
    """Best-effort SMTP notifier built on top of MailClient.

    Credentials default to the shared JAccount credentials provider, matching
    how the rest of the suite authenticates.
    """

    mail: MailClient
    to: str | tuple[str, ...]
    from_addr: str | None = None
    default_subject_prefix: str = "[SJTU]"
    timeout: float = 15.0
    _failures: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.to, str):
            self.to = (self.to,)
        self.to = tuple(addr.strip() for addr in self.to if addr.strip())
        if not self.to:
            raise ValueError("MailNotifier requires at least one recipient.")

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None,
        *,
        username: str | None = None,
        password: str | None = None,
        host: str = "mail.sjtu.edu.cn",
    ) -> "MailNotifier | None":
        """Build a notifier from a `notifications.mail` config block.

        Supported keys:
        - `to`: address or list of addresses (required)
        - `from`: optional From address override
        - `subject_prefix`: optional prefix, default "[SJTU]"
        - `host` / `smtp_port` / `imap_port`: server overrides

        Credentials are resolved through the suite's CredentialProvider when
        not supplied explicitly.
        """
        if not config or not config.get("enabled", True):
            return None

        recipients = config.get("to") or config.get("recipients")
        if isinstance(recipients, str):
            recipients = [item.strip() for item in recipients.split(",") if item.strip()]
        recipients = [str(item).strip() for item in (recipients or []) if str(item).strip()]
        if not recipients:
            return None

        if username is None or password is None:
            from sjtusuite.core.credentials import credentials

            username = username or credentials.username
            password = password or credentials.password
        if not username or not password:
            return None
        mail = MailClient(
            username,
            password,
            host=str(config.get("host") or host),
            smtp_port=int(config.get("smtp_port") or 465),
            imap_port=int(config.get("imap_port") or 993),
        )
        return cls(
            mail=mail,
            to=tuple(recipients),
            from_addr=config.get("from"),
            default_subject_prefix=str(config.get("subject_prefix") or "[SJTU]"),
        )

    def send(
        self,
        message: str,
        *,
        subject: str | None = None,
        to: str | tuple[str, ...] | None = None,
        html: bool = False,
    ) -> MailNotificationResult:
        """Send a notification mail. Returns a result object instead of raising,
        so daemon code can call this unconditionally."""
        recipients = to or self.to
        if isinstance(recipients, str):
            recipients = (recipients,)
        recipients = tuple(addr.strip() for addr in recipients if addr.strip())

        full_subject = subject or "Notification"
        if self.default_subject_prefix and not full_subject.startswith(self.default_subject_prefix):
            full_subject = f"{self.default_subject_prefix} {full_subject}"

        email = EmailMessage()
        email["Subject"] = full_subject
        email["From"] = self.from_addr or self.mail.address
        email["To"] = ", ".join(recipients)
        email.set_content(message, subtype="html" if html else "plain")

        try:
            self.mail.send_message(email)
            self._failures = 0
        except MailError as exc:
            self._failures += 1
            return MailNotificationResult(
                ok=False,
                from_addr=email["From"],
                recipients=email["To"],
                error=str(exc),
            )
        return MailNotificationResult(
            ok=True,
            from_addr=email["From"],
            recipients=email["To"],
        )

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive send failures (reset on the next success)."""
        return self._failures
