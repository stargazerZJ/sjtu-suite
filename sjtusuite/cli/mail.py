"""
SJTU Mail CLI - Terminal UI for the university mailbox (mail.sjtu.edu.cn).

Provides subcommands to:
- Inspect folders and unread counts
- List and read messages (envelope list, then full body)
- Mark messages as read / unread
- Compose and send mail through SMTP
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from sjtusuite.clients.mail import DEFAULT_FOLDERS, MailClient, MailError
from sjtusuite.core.credentials import credentials


console = Console()


def create_client() -> MailClient:
    if not credentials.username or not credentials.password:
        raise click.ClickException("Missing JAccount credentials in credentials.json.")
    return MailClient(credentials.username, credentials.password)


def _handle(exc: Exception) -> None:
    raise click.ClickException(str(exc))


@click.group()
def cli():
    """SJTU Mail CLI - read and send mail on mail.sjtu.edu.cn."""


@cli.command()
def folders():
    """List mailbox folders and unread counts."""
    client = create_client()
    try:
        names = client.list_folders()
    except MailError as exc:
        _handle(exc)

    table = Table(title="Mailbox folders", show_header=True, header_style="bold magenta")
    table.add_column("Folder", style="cyan")
    table.add_column("Unread", justify="right")
    for name in names:
        try:
            unread = client.unread_count(folder=name)
        except MailError:
            unread = "-"
        table.add_row(name, str(unread))
    console.print(table)


@cli.command()
@click.option("--folder", "-f", default="INBOX", help="Folder to list (default: INBOX)")
@click.option("--limit", "-n", default=25, show_default=True, help="Max messages to show")
@click.option("--unread", "-u", is_flag=True, help="Only unread messages")
def list(folder, limit, unread):
    """List recent messages in a folder."""
    client = create_client()
    try:
        messages = client.list_messages(folder=folder, limit=limit, unread_only=unread)
    except MailError as exc:
        _handle(exc)

    if not messages:
        console.print(f"[yellow]No messages in {folder}.[/yellow]")
        return

    table = Table(title=f"{folder} ({len(messages)} shown)", show_header=True, header_style="bold magenta")
    table.add_column("UID", style="dim", justify="right")
    table.add_column("Flags", style="green")
    table.add_column("From")
    table.add_column("Subject", style="cyan")
    table.add_column("Date", style="yellow")
    for m in messages:
        flags = "*" if not m.seen else " "
        subject = m.subject if len(m.subject) <= 60 else m.subject[:57] + "..."
        sender = m.from_ if len(m.from_) <= 35 else m.from_[:32] + "..."
        date = (m.date or "")[:22]
        table.add_row(str(m.uid), flags, sender, subject, date)
    console.print(table)
    console.print("[dim]`sjtu-mail read <UID>` to view a message, `--mark-read` to mark seen.[/dim]")


@cli.command()
@click.argument("uid", type=int)
@click.option("--folder", "-f", default="INBOX", help="Folder containing the message")
@click.option("--mark-read", is_flag=True, help="Mark the message as seen after fetching")
def read(uid, folder, mark_read):
    """Show one message in full."""
    client = create_client()
    try:
        summary = next(
            (m for m in client.search(folder=folder, criteria="ALL") if m.uid == uid),
            None,
        )
        message = client.get_message(uid, folder=folder, mark_seen=mark_read)
        body = client.get_body(uid, folder=folder, mark_seen=False)
    except MailError as exc:
        _handle(exc)

    subject = summary.subject if summary else (message.get("Subject") or "")
    sender = summary.from_ if summary else (message.get("From") or "")
    date = summary.date if summary else (message.get("Date") or "")

    console.print(
        Panel(
            "\n".join(
                [
                    f"[bold]From:[/bold] {sender}",
                    f"[bold]Date:[/bold]  {date}",
                ]
            ),
            title=subject or f"(no subject) UID {uid}",
            border_style="blue",
        )
    )
    if body:
        console.print(body)
    else:
        console.print("[yellow]No text body found (possibly attachments only).[/yellow]")


@cli.command()
@click.argument("uid", type=int)
@click.option("--folder", "-f", default="INBOX", help="Folder containing the message")
@click.option("--unread", is_flag=True, help="Mark as UNREAD instead of read")
def mark(uid, folder, unread):
    """Mark a message as read (or unread with --unread)."""
    client = create_client()
    try:
        ok = client.mark(uid, "\\Seen", folder=folder, add=not unread)
    except MailError as exc:
        _handle(exc)
    if ok:
        console.print(f"[green]UID {uid} marked as {'unread' if unread else 'read'}.[/green]")
    else:
        console.print(f"[red]Failed to mark UID {uid}.[/red]")


@cli.command()
@click.option("--to", "-t", default=None, help="Recipient (defaults to yourself)")
@click.option("--subject", "-s", default=None, help="Subject")
@click.option("--body", "-b", default=None, help="Body text (omit for interactive prompt)")
@click.option("--cc", default=None, help="Cc recipient(s), comma separated")
def send(to, subject, body, cc):
    """Compose and send a message via SMTP."""
    client = create_client()
    recipient = to or Prompt.ask("To", default=client.address)
    title = subject or Prompt.ask("Subject")
    text = body
    if text is None:
        text = Prompt.ask("Body (single line; use \\n for newlines)")
        text = text.replace("\\n", "\n")

    try:
        info = client.send(
            to=recipient,
            subject=title,
            body=text,
            cc=cc,
        )
    except MailError as exc:
        _handle(exc)
    console.print(f"[green]Sent to {info['recipients']}.[/green]")


@cli.command()
def test():
    """Send a test message to yourself to verify SMTP credentials."""
    client = create_client()
    try:
        info = client.send(
            to=client.address,
            subject="sjtu-suite mail test",
            body="Hello from sjtu-suite! SMTP is working.",
        )
    except MailError as exc:
        _handle(exc)
    console.print(f"[green]Test mail sent to {info['recipients']}.[/green]")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
