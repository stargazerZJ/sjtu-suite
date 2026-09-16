"""
SJTU Mail Client - SMTP/IMAP access for mail.sjtu.edu.cn.

The university mail service (mail.sjtu.edu.cn) exposes standard protocols:

- SMTP over SSL on port 465 (submission with JAccount credentials)
- IMAP4rev1 over SSL on port 993 (mailbox access with JAccount credentials)

The same JAccount username/password used by the rest of the suite works for
both protocols, so no separate mail credentials are required.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import parseaddr
from typing import Iterator, Sequence

MAIL_HOST = "mail.sjtu.edu.cn"
SMTP_SSL_PORT = 465
IMAP_SSL_PORT = 993
DEFAULT_DOMAIN = "sjtu.edu.cn"
REQUEST_TIMEOUT_SECONDS = 15.0

DEFAULT_FOLDERS = (
    "INBOX",
    "Archive",
    "Drafts",
    "Junk",
    "Sent",
    "Trash",
)

# Reasonable fetch window defaults.
DEFAULT_LIST_PAGE_SIZE = 25


class MailError(RuntimeError):
    """Raised when an SMTP or IMAP operation fails."""


@dataclass(slots=True, frozen=True)
class MailAddress:
    display_name: str | None
    address: str


@dataclass(slots=True, frozen=True)
class MailSummary:
    """One message envelope (parsed from IMAP FETCH BODY.PEEK[ENVELOPE])."""

    uid: int
    subject: str
    from_: str
    to: str
    date: str
    flags: tuple[str, ...]
    size: int | None = None

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags


@dataclass(slots=True)
class MailClient:
    """SMTP + IMAP client for the SJTU student mail system."""

    username: str
    password: str
    host: str = MAIL_HOST
    smtp_port: int = SMTP_SSL_PORT
    imap_port: int = IMAP_SSL_PORT
    timeout: float = REQUEST_TIMEOUT_SECONDS
    default_domain: str = DEFAULT_DOMAIN
    _imap: imaplib.IMAP4_SSL | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- helpers

    @property
    def address(self) -> str:
        """The user's own mail address (username may or may not include the domain)."""
        if "@" in self.username:
            return self.username
        return f"{self.username}@{self.default_domain}"

    def _smtp_connection(self) -> smtplib.SMTP_SSL:
        try:
            return smtplib.SMTP_SSL(self.host, self.smtp_port, timeout=self.timeout)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(f"Failed to connect to SMTP {self.host}:{self.smtp_port}: {exc}") from exc

    def _imap_connection(self) -> imaplib.IMAP4_SSL:
        try:
            client = imaplib.IMAP4_SSL(self.host, self.imap_port, timeout=self.timeout)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError(f"Failed to connect to IMAP {self.host}:{self.imap_port}: {exc}") from exc
        try:
            client.login(self.username, self.password)
        except imaplib.IMAP4.error as exc:
            try:
                client.logout()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            raise MailError(f"IMAP login failed: {exc}") from exc
        return client

    @staticmethod
    def _decode_header_value(value: str | None) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:  # pragma: no cover - malformed headers
            return value

    # ------------------------------------------------------------------ SMTP

    def send_message(
        self,
        message: EmailMessage,
    ) -> dict[str, str]:
        """Send one message. `From` defaults to the user's own address."""
        if not message.get("From"):
            message["From"] = self.address
        recipients = message.get_all("To", []) + message.get_all("Cc", []) + message.get_all("Bcc", [])
        flat: list[str] = []
        for value in recipients:
            _, addr = parseaddr(str(value))
            if addr:
                flat.append(addr)
        if not flat:
            raise MailError("Message has no recipients.")

        smtp = self._smtp_connection()
        try:
            smtp.login(self.username, self.password)
            smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"SMTP authentication failed: {exc}") from exc
        except smtplib.SMTPException as exc:
            raise MailError(f"SMTP send failed: {exc}") from exc
        finally:
            try:
                smtp.quit()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        return {"from": self.address, "recipients": ", ".join(flat)}

    def send(
        self,
        to: str | Sequence[str],
        subject: str,
        body: str,
        *,
        cc: str | Sequence[str] | None = None,
        html: bool = False,
    ) -> dict[str, str]:
        """Compose and send a simple text or HTML message."""
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.address
        if isinstance(to, str):
            message["To"] = to
        else:
            message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
        message.set_content(body, subtype="html" if html else "plain")
        return self.send_message(message)

    # ------------------------------------------------------------------ IMAP

    def list_folders(self) -> list[str]:
        """List mailbox folder names (INBOX, Sent, ...)."""
        imap = self._imap_connection()
        try:
            typ, boxes = imap.list()
            if typ != "OK":
                raise MailError(f"IMAP LIST failed: {typ}")
            names = []
            for raw in boxes or []:
                parts = (raw.decode() if isinstance(raw, bytes) else str(raw)).rsplit(' "/" ', 1)
                names.append(self._decode_header_value(parts[-1]).strip('"'))
            return names
        finally:
            self._close_imap(imap)

    def search(
        self,
        *,
        folder: str = "INBOX",
        criteria: str = "ALL",
        readonly: bool = True,
    ) -> list[MailSummary]:
        """Search a folder and return message envelopes (newest first)."""
        imap = self._open_folder(folder, readonly=readonly)
        try:
            typ, data = imap.uid("search", None, criteria)
            if typ != "OK":
                raise MailError(f"IMAP SEARCH failed: {typ}")
            uids = (data[0] or b"").split()
            if not uids:
                return []
            # Fetch in batches so huge mailboxes don't blow up one command.
            summaries: list[MailSummary] = []
            for start in range(0, len(uids), 200):
                batch = b",".join(uids[start : start + 200])
                summaries.extend(self._fetch_envelopes(imap, batch))
            summaries.sort(key=lambda s: s.uid, reverse=True)
            return summaries
        finally:
            self._close_imap(imap)

    def list_messages(
        self,
        *,
        folder: str = "INBOX",
        limit: int = DEFAULT_LIST_PAGE_SIZE,
        unread_only: bool = False,
    ) -> list[MailSummary]:
        """Newest-first message envelopes from a folder (optionally unread only)."""
        criteria = "UNSEEN" if unread_only else "ALL"
        return self.search(folder=folder, criteria=criteria)[:limit]

    def unread_count(self, *, folder: str = "INBOX") -> int:
        """Number of unread messages in a folder."""
        imap = self._open_folder(folder, readonly=True)
        try:
            typ, data = imap.status(f'"{folder}"', "(UNSEEN)")
            if typ != "OK" or not data or not data[0]:
                raise MailError(f"IMAP STATUS failed: {typ}")
            text = data[0].decode() if isinstance(data[0], bytes) else str(data[0])
            # e.g. 'INBOX (UNSEEN 437)'
            try:
                return int(text.rsplit("UNSEEN", 1)[1].strip(" ()\r\n"))
            except (IndexError, ValueError) as exc:
                raise MailError(f"Unexpected IMAP STATUS response: {text}") from exc
        finally:
            self._close_imap(imap)

    def get_message(
        self,
        uid: int,
        *,
        folder: str = "INBOX",
        mark_seen: bool = False,
        save_session: bool = True,
    ) -> Message:
        """Fetch a full message by UID."""
        imap = self._open_folder(folder, readonly=not mark_seen)
        try:
            typ, data = imap.uid("fetch", str(uid), "(BODY.PEEK[] RFC822.SIZE INTERNALDATE FLAGS)")
            if typ != "OK" or not data or not data[0]:
                raise MailError(f"IMAP FETCH failed for UID {uid}: {typ}")
            raw = None
            for item in data:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                    raw = bytes(item[1])
                    break
            if raw is None:
                raise MailError(f"IMAP FETCH returned no body for UID {uid}.")
            from email import message_from_bytes

            return message_from_bytes(raw)
        finally:
            self._close_imap(imap)

    def get_body(self, uid: int, *, folder: str = "INBOX", mark_seen: bool = False) -> str:
        """Fetch a message and extract its best text body (text/plain or text/html)."""
        message = self.get_message(uid, folder=folder, mark_seen=mark_seen)
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, errors="replace")
            return ""
        payload = message.get_payload(decode=True)
        if payload:
            charset = message.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
        return ""

    def mark(self, uid: int, flag: str, *, folder: str = "INBOX", add: bool = True) -> bool:
        """Add or remove an IMAP flag (e.g. \\Seen, \\Flagged) on one message."""
        imap = self._open_folder(folder, readonly=False)
        try:
            action = "+FLAGS.SILENT" if add else "-FLAGS.SILENT"
            typ, _ = imap.uid("store", str(uid), action, f"({flag})")
            return typ == "OK"
        finally:
            self._close_imap(imap)

    def mark_read(self, uid: int, *, folder: str = "INBOX") -> bool:
        return self.mark(uid, "\\Seen", folder=folder, add=True)

    def iter_unread(self, *, folder: str = "INBOX") -> Iterator[MailSummary]:
        """Yield unread envelopes without pagination concerns."""
        yield from self.search(folder=folder, criteria="UNSEEN")

    # ------------------------------------------------------- IMAP plumbing

    def _open_folder(self, folder: str, *, readonly: bool) -> imaplib.IMAP4_SSL:
        imap = self._imap_connection()
        try:
            typ, _ = imap.select(f'"{folder}"', readonly=readonly)
            if typ != "OK":
                raise MailError(f"IMAP SELECT failed for folder {folder!r}: {typ}")
        except MailError:
            self._close_imap(imap)
            raise
        return imap

    def _close_imap(self, imap: imaplib.IMAP4_SSL) -> None:
        try:
            imap.logout()
        except Exception:  # pragma: no cover - best effort cleanup
            pass

    def _fetch_envelopes(self, imap: imaplib.IMAP4_SSL, uid_batch: bytes) -> list[MailSummary]:
        typ, data = imap.uid("fetch", uid_batch, "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
        if typ != "OK" or not data:
            raise MailError(f"IMAP FETCH failed: {typ}")
        summaries: list[MailSummary] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            header_bytes = item[1]
            if isinstance(header_bytes, (bytes, bytearray)):
                text = bytes(header_bytes).decode("utf-8", errors="replace")
            else:  # pragma: no cover - defensive
                text = str(header_bytes)

            from email.parser import HeaderParser

            msg = HeaderParser().parsestr(text)
            meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
            uid = self._extract_uid(meta)
            flags = self._extract_flags(meta)
            summaries.append(
                MailSummary(
                    uid=uid,
                    subject=self._decode_header_value(msg.get("Subject")),
                    from_=self._decode_header_value(msg.get("From")),
                    to=self._decode_header_value(msg.get("To")),
                    date=self._decode_header_value(msg.get("Date")),
                    flags=tuple(flags),
                    size=self._extract_size(meta),
                )
            )
        return summaries

    @staticmethod
    def _extract_uid(meta: str) -> int:
        # e.g. '1 (UID 12345 FLAGS (\\Seen) RFC822.SIZE 4096 ...)'
        match = re.search(r"UID (\d+)", meta)
        if not match:
            raise MailError(f"Could not parse UID from IMAP response: {meta}")
        return int(match.group(1))

    @staticmethod
    def _extract_flags(meta: str) -> list[str]:
        match = re.search(r"FLAGS \(([^)]*)\)", meta)
        if not match:
            return []
        return match.group(1).split()

    @staticmethod
    def _extract_size(meta: str) -> int | None:
        match = re.search(r"RFC822\.SIZE (\d+)", meta)
        return int(match.group(1)) if match else None


if __name__ == "__main__":
    from sjtusuite.core.credentials import credentials

    client = MailClient(credentials.username, credentials.password)
    print("folders:", client.list_folders())
    print("unread (INBOX):", client.unread_count())
    for summary in client.list_messages(limit=5):
        print(summary.uid, summary.subject, "|", summary.from_)
