# University mailbox (mail.sjtu.edu.cn)

Read and send mail over standard IMAP/SMTP with the same JAccount
credentials. Module: `sjtusuite/clients/mail.py` (`MailClient`,
`MailError`, `MailSummary`); notifier in `sjtusuite/notifications/mail.py`
(`MailNotifier`). No OAuth/cookies involved — plain SSL connections
(SMTP 465, IMAP 993), fresh connection per operation.

## CLI

```bash
uv run sjtu-mail folders                       # folders + unread counts
uv run sjtu-mail list -n 20                    # newest in INBOX
uv run sjtu-mail list --folder Sent --unread   # unread in another folder
uv run sjtu-mail read 2046                     # print message body
uv run sjtu-mail read 2046 --mark-read         # also mark seen
uv run sjtu-mail mark 2046 --unread            # flip flag
uv run sjtu-mail send -t name@sjtu.edu.cn -s "Hi" -b "Body"
uv run sjtu-mail send -t name@sjtu.edu.cn -s "Hi" -b "Body" --cc a@sjtu.edu.cn,b@sjtu.edu.cn
uv run sjtu-mail test                          # self-test mail to yourself
```

`send` without `-b` prompts for the body interactively — always pass `-b`
in agent contexts. Omitting `-t` sends to yourself. `--cc` is a single
comma-separated string. For multiple To recipients use the Python API.

## Python API

```python
from sjtusuite.clients import MailClient

mail = MailClient(username, password)   # same JAccount creds

# Read
mail.list_folders()
mail.unread_count()                     # INBOX default
mail.list_messages(limit=20)            # newest-first MailSummary (.uid, .subject, ...)
mail.list_messages(folder="Sent", unread_only=True)
mail.search(folder="INBOX", criteria='SUBJECT "成绩"')   # raw IMAP criteria
body = mail.get_body(uid)               # full text of one message
mail.get_message(uid)                   # full email.message.Message
mail.mark_read(uid) / mail.mark(uid, "unread", add=False)
mail.iter_unread()

# Send
mail.send(to="someone@sjtu.edu.cn", subject="Hi", body="...")
mail.send(to=["a@sjtu.edu.cn", "b@sjtu.edu.cn"], subject="Hi",
          body="<b>Hi</b>", html=True)
```

`MailSummary` fields: `uid`, `subject`, `from_`, `to`, `date`, `size`,
`seen`. UIDs are IMAP UIDs (stable within a folder, not global — the CLI
prints them).

## As a notification channel (never raises)

```python
from sjtusuite.clients import MailClient
from sjtusuite.notifications import MailNotifier

notifier = MailNotifier(MailClient(username, password), to="me@sjtu.edu.cn")
result = notifier.send("Reservation confirmed", subject="Sports booked")
result.sent   # bool; failures are reported, not raised
```

Daemons build this from the `notifications.mail` block of
`credentials.json` (minimal: `{"to": ["me@sjtu.edu.cn"]}` — creds default
to the shared JAccount ones).

Gotchas:
- Sending real mail is an externally-visible action — confirm the recipient
  and content with the user first; use `sjtu-mail test` for connectivity
  checks.
- Chinese/mime headers are decoded by the client; if subjects look raw,
  you're reading the `Message` object directly instead of `MailSummary`.
- 2FA on the JAccount can block the IMAP/SMTP password login in some
  account configurations — if auth fails with valid creds, this is why.
