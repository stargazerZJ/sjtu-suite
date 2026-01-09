"""
SJTU PE CLI - Terminal interface for PE running simulation.

Commands:
- run: Simulate a running session with customizable parameters
- status: Check PE system login status
"""

import click
from datetime import datetime, timedelta
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from sjtusuite.auth import JACLogin
from sjtusuite.clients.pe import PEClient
from sjtusuite.core.credentials import credentials


console = Console()


def create_client():
    """Initialize and return authenticated PE client."""
    jac_login = JACLogin(credentials.username, credentials.password)
    return PEClient(jac_login)


@click.group()
def cli():
    """SJTU PE System CLI - Running simulation tool."""
    pass


@cli.command()
@click.option('--distance', '-d', type=float, default=2.0,
              help='Target running distance in km (default: 2.0)(approximate)')
@click.option('--time', '-t', type=str, default=None,
              help='Run time in ISO format (default: 30 minutes ago)')
@click.option('--points', '-n', type=int, default=None,
              help='Number of GPS points (default: auto from distance)')
@click.option('--dry-run', is_flag=True, help='Show what would be done without uploading')
def run(distance, time, points, dry_run):
    """Simulate a running session and upload to PE system.
    
    Examples:
    
        sjtu-pe run                    # Default 2km run, 30 min ago
        sjtu-pe run -d 3.0             # 3km run
        sjtu-pe run -t "2024-01-07 08:00"  # Specific time
        sjtu-pe run --dry-run          # Preview without uploading
    """
    console.print(Panel.fit(
        "[bold blue]SJTU PE Running Simulator[/bold blue]\n"
        "[dim]Upload simulated running data to PE system[/dim]",
        border_style="blue"
    ))
    
    # Calculate points from distance if not specified
    if points is None:
        points = int(distance * 6500)  # ~6500 points per km
    
    # Parse time or default to 30 minutes ago
    if time:
        try:
            run_time = datetime.fromisoformat(time)
        except ValueError:
            console.print(f"[red]Invalid time format: {time}[/red]")
            console.print("[dim]Use ISO format like: 2024-01-07 08:00[/dim]")
            return
    else:
        run_time = datetime.now() - timedelta(minutes=30)
    
    # Display parameters
    table = Table(title="Run Parameters", show_header=False, box=None)
    table.add_column("Parameter", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Distance", f"{distance:.1f} km")
    table.add_row("GPS Points", str(points))
    table.add_row("Run Time", run_time.strftime("%Y-%m-%d %H:%M"))
    console.print(table)
    console.print()
    
    if dry_run:
        console.print("[yellow]Dry run mode - no data will be uploaded.[/yellow]")
        return
    
    # Initialize client
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Initializing PE client...", total=None)
        client = create_client()
        progress.update(task, description="[green]✓ Client initialized[/green]")
    
    # Login
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Logging in to PE system...", total=None)
        try:
            if client.login():
                progress.update(task, description="[green]✓ Login successful[/green]")
            else:
                progress.update(task, description="[red]✗ Login failed[/red]")
                return
        except Exception as e:
            console.print(f"[red]Login error: {e}[/red]")
            return
    
    # Get UID
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Getting user ID...", total=None)
        try:
            uid = client.get_uid()
            progress.update(task, description=f"[green]✓ User ID: {uid}[/green]")
        except Exception as e:
            console.print(f"[red]Failed to get UID: {e}[/red]")
            return
    
    # Simulate running
    console.print()
    console.print("[bold]Starting running simulation...[/bold]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Uploading running data...", total=None)
        try:
            response = client.simulate_running(run_time=run_time, n=points)
            
            if response and response.status_code == 200 and response.json().get('code') == 0:
                progress.update(task, description="[green]✓ Upload successful![/green]")
                console.print()
                console.print(Panel(
                    f"[bold green]✓ Running simulation completed![/bold green]\n\n"
                    f"Distance (Approx): {distance:.1f} km\n"
                    f"Time: {run_time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"Points: {points}",
                    title="Success",
                    border_style="green"
                ))
                
                # Try to parse response
                try:
                    resp_data = response.json()
                    if resp_data.get('code') == 0:
                        console.print("[green]Server confirmed: Upload accepted[/green]")
                    else:
                        console.print(f"[yellow]Server response: {resp_data.get('message', 'Unknown')}[/yellow]")
                except:
                    pass
            else:
                error_msg = response.text if response else "No response"
                progress.update(task, description="[red]✗ Upload failed[/red]")
                console.print(f"[red]Error: {error_msg}[/red]")
        except Exception as e:
            console.print(f"[red]Simulation error: {e}[/red]")


@cli.command()
def status():
    """Check PE system login status."""
    console.print(Panel.fit(
        "[bold blue]PE System Status Check[/bold blue]",
        border_style="blue"
    ))
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Checking status...", total=None)
        client = create_client()
        
        logged_in = client.validate_session()
        
        if logged_in:
            progress.update(task, description="[green]✓ Session valid[/green]")
            try:
                uid = client.get_uid()
                console.print(f"\n[green]Logged in as: {uid}[/green]")
            except:
                console.print("\n[green]Session is valid[/green]")
        else:
            progress.update(task, description="[yellow]Session expired[/yellow]")
            console.print("\n[yellow]Session expired - will re-login on next command[/yellow]")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()
