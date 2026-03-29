"""CLI for the SJTU sports venue reservation system."""

from __future__ import annotations

import json

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sjtusuite.auth import JACLogin
from sjtusuite.clients.sports import SportsAPIError, SportsReservationClient
from sjtusuite.core.credentials import credentials


console = Console()


def create_client(from_browser: bool = False) -> SportsReservationClient:
    jac_login = JACLogin(credentials.username or "", credentials.password or "")
    client = SportsReservationClient(jac_login)
    if from_browser:
        try:
            client.load_playwright_browser_session()
        except SportsAPIError as exc:
            raise click.ClickException(str(exc)) from exc
        return client
    if not credentials.username or not credentials.password:
        raise click.ClickException(
            "Missing username/password in credentials.json or environment. "
            "Use --from-browser after logging in to the Playwright browser if you want to borrow that session."
        )
    client.login()
    return client


@click.group()
@click.option(
    "--from-browser",
    is_flag=True,
    help="Use the current Playwright browser session instead of credentials.json.",
)
@click.pass_context
def cli(ctx: click.Context, from_browser: bool):
    """SJTU sports reservation tools."""
    ctx.ensure_object(dict)
    ctx.obj["from_browser"] = from_browser


@cli.command()
@click.option("--search", default="", help="Venue name search string.")
@click.option("--page-size", default=12, show_default=True, help="Number of rows to fetch.")
@click.option("--page-num", default=1, show_default=True, help="Pagination page number.")
@click.pass_context
def venues(ctx: click.Context, search: str, page_size: int, page_num: int):
    """List personal-booking venues."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    venues, total = client.list_venues(page_num=page_num, page_size=page_size, venue_name=search)

    table = Table(title=f"Sports venues (total: {total})")
    table.add_column("Venue ID", style="cyan")
    table.add_column("Venue")
    table.add_column("Campus", style="green")
    table.add_column("Open time", style="yellow")

    for venue in venues:
        table.add_row(venue.venue_id, venue.venue_name, venue.campus_name, venue.open_time)
    console.print(table)


@cli.command()
@click.argument("venue_id")
@click.pass_context
def detail(ctx: click.Context, venue_id: str):
    """Show venue detail and motion types."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    detail = client.get_venue_detail(venue_id)

    console.print(
        Panel.fit(
            f"[bold]{detail.venue_name}[/bold]\n"
            f"Campus: {detail.campus_name}\n"
            f"Open time: {detail.open_time}\n"
            f"Phone: {detail.venue_mobile}",
            title="Venue detail",
            border_style="blue",
        )
    )

    table = Table(title="Motion types")
    table.add_column("Motion ID", style="cyan")
    table.add_column("Name")
    table.add_column("Tension", style="yellow")
    table.add_column("Write-off type", style="green")
    for motion in detail.motion_types:
        table.add_row(
            motion.id,
            motion.name,
            str(motion.tension),
            str(motion.write_off_type),
        )
    console.print(table)


@cli.command()
@click.argument("venue_id")
@click.option("--motion", required=True, help="Motion type ID or exact motion type name.")
@click.pass_context
def availability(ctx: click.Context, venue_id: str, motion: str):
    """Show date-level availability counts for a venue motion type."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    motion_type = client.resolve_motion_type(venue_id, motion)
    date_options = client.list_date_options(venue_id, motion_type.id)

    table = Table(title=f"Availability for {motion_type.name}")
    table.add_column("Date", style="cyan")
    table.add_column("Visible label")
    table.add_column("Selectable slots", style="green")

    for option in date_options:
        count = len(
            client.list_available_slots(
                venue_id,
                motion_type.id,
                date=option.date,
                date_id=option.date_id,
            )
        )
        table.add_row(option.date, option.view_str, str(count))
    console.print(table)


@cli.command()
@click.argument("venue_id")
@click.option("--motion", required=True, help="Motion type ID or exact motion type name.")
@click.option("--date", "date_str", required=True, help="Reservation date, e.g. 2026-03-29.")
@click.option("--field", "field_names", multiple=True, required=True, help="Field/court name. Repeat for multiple.")
@click.option("--time", "time_slots", multiple=True, required=True, help="Time slot, e.g. 19:00-20:00. Repeat for multiple.")
@click.option("--submit", is_flag=True, help="Actually create the order instead of only previewing it.")
@click.option("--captcha-verification", default=None, help="Optional captcha verification string when the API requires it.")
@click.option("--create-payment", is_flag=True, help="After successful submit, request the payment URL.")
@click.pass_context
def reserve(
    ctx: click.Context,
    venue_id: str,
    motion: str,
    date_str: str,
    field_names: tuple[str, ...],
    time_slots: tuple[str, ...],
    submit: bool,
    captcha_verification: str | None,
    create_payment: bool,
):
    """Preview or submit a personal sports reservation."""
    client = create_client(from_browser=ctx.obj["from_browser"])

    try:
        preview = client.preview_personal_order(
            venue_id=venue_id,
            motion=motion,
            date=date_str,
            field_names=field_names,
            time_slots=time_slots,
        )
    except SportsAPIError as exc:
        raise click.ClickException(str(exc)) from exc

    detail = client.get_venue_detail(venue_id)
    slots_table = Table(title=f"Selected slots for {detail.venue_name}")
    slots_table.add_column("Field", style="cyan")
    slots_table.add_column("Time")
    slots_table.add_column("Price", style="green")
    for slot in preview["selected_slots"]:
        slots_table.add_row(slot.field_name, slot.time_slot, slot.price)
    console.print(slots_table)

    console.print(
        Panel.fit(
            f"Venue: {detail.venue_name}\n"
            f"Motion: {preview['motion_type'].name}\n"
            f"Date: {preview['date_option'].date}\n"
            f"Total: {preview['total_price']}",
            title="Reservation preview",
            border_style="blue",
        )
    )

    console.print("[bold]ConfirmOrder payload preview[/bold]")
    console.print_json(
        json.dumps(
            preview["confirm_order_payload"],
            ensure_ascii=False,
            indent=2,
        )
    )

    if not submit:
        terms = client.get_terms()
        console.print(
            Panel.fit(
                f"[bold]{terms.get('title', 'Terms')}[/bold]\n"
                "[dim]Fetched current clause text from /venue/moreSetting/getOne.[/dim]\n"
                "Run again with --submit to create the order.",
                title="Dry run only",
                border_style="yellow",
            )
        )
        return

    response = client.confirm_personal_order(
        preview["confirm_order_payload"],
        captcha_verification=captcha_verification,
    )

    console.print("[bold]ConfirmOrder response[/bold]")
    console.print_json(json.dumps(response, ensure_ascii=False, indent=2))

    if response.get("code") != 0:
        if response.get("code") == 1002:
            console.print(
                "[yellow]Captcha verification is required. Supply --captcha-verification after solving the slider challenge.[/yellow]"
            )
        return

    order_id = response["data"]
    console.print(f"[green]Order created successfully: {order_id}[/green]")

    if create_payment:
        payment = client.create_payment(order_id)
        console.print("[bold]Payment initialization response[/bold]")
        console.print_json(json.dumps(payment, ensure_ascii=False, indent=2))
        if payment.get("payment_url"):
            console.print(f"[green]Payment URL:[/green] {payment['payment_url']}")


def main():
    cli()


if __name__ == "__main__":
    main()
