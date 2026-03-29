"""CLI for the SJTU sports venue reservation system."""

from __future__ import annotations

import json
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from sjtusuite.auth import JACLogin
from sjtusuite.clients.sports import MotionType, SportsAPIError, SportsReservationClient, VenueDetail, VenueSummary
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


def _render_venues_table(venues: list[VenueSummary], title: str, *, include_index: bool = False):
    table = Table(title=title)
    if include_index:
        table.add_column("#", style="magenta")
    table.add_column("Venue ID", style="cyan")
    table.add_column("Venue")
    table.add_column("Campus", style="green")
    table.add_column("Open time", style="yellow")
    for index, venue in enumerate(venues, start=1):
        row = []
        if include_index:
            row.append(str(index))
        row.extend([venue.venue_id, venue.venue_name, venue.campus_name, venue.open_time])
        table.add_row(*row)
    console.print(table)


def _render_venue_detail(detail: VenueDetail):
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


def _render_motion_types(detail: VenueDetail, *, include_index: bool = False):
    table = Table(title="Motion types")
    if include_index:
        table.add_column("#", style="magenta")
    table.add_column("Motion ID", style="cyan")
    table.add_column("Name")
    table.add_column("Tension", style="yellow")
    table.add_column("Write-off type", style="green")
    for index, motion in enumerate(detail.motion_types, start=1):
        row = []
        if include_index:
            row.append(str(index))
        row.extend(
            [
                motion.id,
                motion.name,
                str(motion.tension),
                str(motion.write_off_type),
            ]
        )
        table.add_row(*row)
    console.print(table)


def _collect_availability_rows(client: SportsReservationClient, venue_id: str, motion_type: MotionType):
    date_options = client.list_date_options(venue_id, motion_type.id)
    selectable_by_date: list[tuple[Any, list[Any]]] = []
    for option in date_options:
        slots = client.list_available_slots(
            venue_id,
            motion_type.id,
            date=option.date,
            date_id=option.date_id,
        )
        selectable_by_date.append((option, slots))
    return selectable_by_date


def _render_availability_tables(motion_name: str, selectable_by_date):
    table = Table(title=f"Availability for {motion_name}")
    table.add_column("Date", style="cyan")
    table.add_column("Visible label")
    table.add_column("Selectable slots", style="green")
    for option, slots in selectable_by_date:
        table.add_row(option.date, option.view_str, str(len(slots)))
    console.print(table)

    detail_rows = [
        (option.date, option.view_str, slot)
        for option, slots in selectable_by_date
        for slot in slots
    ]
    if not detail_rows:
        console.print("[yellow]No selectable slots are currently exposed in the visible reservation window.[/yellow]")
        return

    detail_table = Table(title=f"Selectable slots for {motion_name}")
    detail_table.add_column("Date", style="cyan")
    detail_table.add_column("Label")
    detail_table.add_column("Field", style="green")
    detail_table.add_column("Time")
    detail_table.add_column("Price", style="yellow")
    for date_str, view_str, slot in detail_rows:
        detail_table.add_row(
            date_str,
            view_str,
            slot.field_name,
            slot.time_slot,
            slot.price,
        )
    console.print(detail_table)


def _render_preview(detail: VenueDetail, preview: dict[str, object]):
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


def _build_preview_from_exact_slots(
    client: SportsReservationClient,
    *,
    venue_id: str,
    motion: MotionType,
    date_option,
    selected_slots: list[Any],
) -> dict[str, object]:
    selected_spaces = client.build_selected_spaces(selected_slots)
    payload = client.build_confirm_order_payload(
        venue_id=venue_id,
        motion_type=motion,
        date_option=date_option,
        selected_spaces=selected_spaces,
    )
    total = sum(float(space["venuePrice"]) for space in selected_spaces)
    return {
        "motion_type": motion,
        "date_option": date_option,
        "selected_slots": selected_slots,
        "selected_spaces": selected_spaces,
        "confirm_order_payload": payload,
        "total_price": f"{total:.2f}",
    }


def _interactive_flow(from_browser: bool):
    client = create_client(from_browser=from_browser)
    console.print(
        Panel.fit(
            "[bold blue]SJTU Sports Interactive[/bold blue]\n"
            "[dim]Browse venues, inspect live slots, and build a reservation step by step.[/dim]",
            border_style="blue",
        )
    )

    search = Prompt.ask("Venue search", default="")
    venues, reported_total = client.list_all_venues(venue_name=search)
    if not venues:
        console.print("[yellow]No venues matched your search.[/yellow]")
        return
    venue_title = f"Sports venues (fetched: {len(venues)}, API said: {reported_total})" if reported_total and reported_total != len(venues) else f"Sports venues (total: {len(venues)})"
    _render_venues_table(venues, venue_title, include_index=True)
    venue_index = IntPrompt.ask("Choose a venue number", choices=[str(i) for i in range(1, len(venues) + 1)])
    venue = venues[venue_index - 1]

    detail = client.get_venue_detail(venue.venue_id)
    _render_venue_detail(detail)
    _render_motion_types(detail, include_index=True)
    motion_index = IntPrompt.ask(
        "Choose a motion type number",
        choices=[str(i) for i in range(1, len(detail.motion_types) + 1)],
    )
    motion = detail.motion_types[motion_index - 1]

    selectable_by_date = _collect_availability_rows(client, venue.venue_id, motion)
    _render_availability_tables(motion.name, selectable_by_date)

    valid_dates = [(option, slots) for option, slots in selectable_by_date if slots]
    if not valid_dates:
        return

    date_table = Table(title="Dates with selectable slots")
    date_table.add_column("#", style="magenta")
    date_table.add_column("Date", style="cyan")
    date_table.add_column("Label")
    date_table.add_column("Selectable", style="green")
    for index, (option, slots) in enumerate(valid_dates, start=1):
        date_table.add_row(str(index), option.date, option.view_str, str(len(slots)))
    console.print(date_table)

    date_index = IntPrompt.ask(
        "Choose a reservation date number",
        choices=[str(i) for i in range(1, len(valid_dates) + 1)],
    )
    selected_option, selected_slots = valid_dates[date_index - 1]

    slot_table = Table(title=f"Selectable slots on {selected_option.date}")
    slot_table.add_column("#", style="magenta")
    slot_table.add_column("Field", style="green")
    slot_table.add_column("Time")
    slot_table.add_column("Price", style="yellow")
    for index, slot in enumerate(selected_slots, start=1):
        slot_table.add_row(str(index), slot.field_name, slot.time_slot, slot.price)
    console.print(slot_table)

    slot_choice = Prompt.ask("Choose one or more slot numbers, comma separated", default="1")
    selected_indices: list[int] = []
    for raw_item in slot_choice.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if not item.isdigit():
            raise click.ClickException(f"Invalid slot selection: {item}")
        index = int(item)
        if index < 1 or index > len(selected_slots):
            raise click.ClickException(f"Slot number out of range: {index}")
        if index not in selected_indices:
            selected_indices.append(index)
    chosen_slots = [selected_slots[index - 1] for index in selected_indices]

    preview = _build_preview_from_exact_slots(
        client,
        venue_id=venue.venue_id,
        motion=motion,
        date_option=selected_option,
        selected_slots=chosen_slots,
    )
    _render_preview(detail, preview)

    if not Confirm.ask("Submit this order now?", default=False):
        terms = client.get_terms()
        console.print(
            Panel.fit(
                f"[bold]{terms.get('title', 'Terms')}[/bold]\n"
                "[dim]Fetched current clause text from /venue/moreSetting/getOne.[/dim]\n"
                "Interactive session ended without submitting an order.",
                title="Dry run only",
                border_style="yellow",
            )
        )
        return

    response = client.confirm_personal_order(preview["confirm_order_payload"])
    console.print("[bold]ConfirmOrder response[/bold]")
    console.print_json(json.dumps(response, ensure_ascii=False, indent=2))
    if response.get("code") != 0:
        if response.get("code") == 1002:
            console.print("[yellow]Captcha verification is required. Please use the explicit reserve command with --captcha-verification after solving it.[/yellow]")
        return

    order_id = response["data"]
    console.print(f"[green]Order created successfully: {order_id}[/green]")
    if Confirm.ask("Initialize payment URL now?", default=True):
        payment = client.create_payment(order_id)
        console.print("[bold]Payment initialization response[/bold]")
        console.print_json(json.dumps(payment, ensure_ascii=False, indent=2))
        if payment.get("payment_url"):
            console.print(f"[green]Payment URL:[/green] {payment['payment_url']}")


@click.group(invoke_without_command=True)
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
    if ctx.invoked_subcommand is None:
        _interactive_flow(from_browser=from_browser)


@cli.command()
@click.option("--search", default="", help="Venue name search string.")
@click.option("--page-size", default=12, show_default=True, help="Number of rows to fetch.")
@click.option("--page-num", default=None, type=int, help="Fetch only one pagination page instead of all pages.")
@click.pass_context
def venues(ctx: click.Context, search: str, page_size: int, page_num: int | None):
    """List personal-booking venues."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    if page_num is not None:
        venues, total = client.list_venues(page_num=page_num, page_size=page_size, venue_name=search)
        title = f"Sports venues (page {page_num}, total: {total})"
    else:
        venues, reported_total = client.list_all_venues(
            page_size=page_size,
            venue_name=search,
        )
        total = len(venues)
        if reported_total and reported_total != total:
            title = f"Sports venues (fetched: {total}, API said: {reported_total})"
        else:
            title = f"Sports venues (total: {total})"

    _render_venues_table(venues, title)


@cli.command()
@click.argument("venue_id")
@click.pass_context
def detail(ctx: click.Context, venue_id: str):
    """Show venue detail and motion types."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    detail = client.get_venue_detail(venue_id)
    _render_venue_detail(detail)
    _render_motion_types(detail)


@cli.command()
@click.argument("venue_id")
@click.option("--motion", required=True, help="Motion type ID or exact motion type name.")
@click.pass_context
def availability(ctx: click.Context, venue_id: str, motion: str):
    """Show date-level availability and the live selectable slots for each date."""
    client = create_client(from_browser=ctx.obj["from_browser"])
    motion_type = client.resolve_motion_type(venue_id, motion)
    selectable_by_date = _collect_availability_rows(client, venue_id, motion_type)
    _render_availability_tables(motion_type.name, selectable_by_date)


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
    _render_preview(detail, preview)

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
