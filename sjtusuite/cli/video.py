"""
SJTU Video CLI - Terminal UI for browsing and downloading course video recordings.

Provides an interactive interface to:
- View enrolled courses from Canvas
- List video sessions for each course
- Get download URLs for video recordings
"""

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

from sjtusuite.auth import JACLogin
from sjtusuite.clients.canvas import CanvasClient
from sjtusuite.clients.video import VideoClient
from sjtusuite.core.config import load_credentials


console = Console()


def create_clients():
    """Initialize and return authenticated clients."""
    credentials = load_credentials()
    jac_login = JACLogin(credentials['username'], credentials['password'])
    canvas_client = CanvasClient(jac_login)
    video_client = VideoClient(jac_login)
    return canvas_client, video_client


def display_courses(courses):
    """Display courses in a formatted table."""
    table = Table(title="Enrolled Courses with Video", show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Course Name", style="cyan")
    table.add_column("Course ID", style="dim")
    
    for idx, course in enumerate(courses, 1):
        table.add_row(str(idx), course['name'], str(course['id']))
    
    console.print(table)


def display_sessions(sessions, course_name):
    """Display video sessions grouped by date."""
    if not sessions:
        console.print("[yellow]No video sessions found for this course.[/yellow]")
        return
    
    # Group by date
    groups = {}
    for s in sessions:
        date_time = s.get('courseBeginTime', '')
        date_part = date_time.split(' ')[0] if ' ' in date_time else date_time.split('T')[0] if 'T' in date_time else 'Unknown'
        if date_part not in groups:
            groups[date_part] = []
        groups[date_part].append(s)
    
    sorted_dates = sorted(groups.keys(), reverse=True)
    
    console.print(Panel(f"[bold]{course_name}[/bold] - {len(sessions)} videos", style="blue"))
    
    session_list = []
    idx = 1
    for date in sorted_dates:
        console.print(f"\n[bold yellow]{date}[/bold yellow] ({len(groups[date])} videos)")
        for s in groups[date]:
            video_name = s.get('videoName') or s.get('name') or 'Unnamed Video'
            video_time = s.get('courseBeginTime', '')
            console.print(f"  [dim]{idx:3}.[/dim] {video_name}")
            session_list.append(s)
            idx += 1
    
    return session_list


def display_video_channels(video_info, channels):
    """Display download channels for a video."""
    video_name = video_info.get('videName', 'Unknown Video')
    
    console.print(Panel(f"[bold]{video_name}[/bold]", title="🎬 Video Details", style="green"))
    
    if not channels:
        console.print("[yellow]No download channels available.[/yellow]")
        return
    
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Channel", style="cyan")
    table.add_column("URL", style="blue", overflow="fold")
    
    for idx, (channel, url, desc) in enumerate(channels, 1):
        table.add_row(str(idx), desc, url)
    
    console.print(table)
    console.print("\n[dim]Copy the URL to download the video.[/dim]")


@click.command()
@click.option('--course-id', '-c', type=int, help='Directly access a specific course by ID')
def main(course_id):
    """Interactive CLI for browsing and downloading SJTU course videos."""
    console.print(Panel.fit(
        "[bold blue]SJTU Video Download Tool[/bold blue]\n"
        "[dim]Browse courses and download video recordings[/dim]",
        border_style="blue"
    ))
    
    # Initialize clients
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(description="Initializing...", total=None)
        canvas_client, video_client = create_clients()
    
    # Login to Canvas
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Logging in to Canvas...", total=None)
        try:
            canvas_client.login()
            progress.update(task, description="[green]✓ Canvas login successful[/green]")
        except Exception as e:
            console.print(f"[red]Canvas login failed: {e}[/red]")
            return
    
    # Get courses with video
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(description="Fetching courses...", total=None)
        try:
            courses = canvas_client.get_courses_with_video()
            progress.update(task, description=f"[green]✓ Found {len(courses)} courses with video[/green]")
        except Exception as e:
            console.print(f"[red]Failed to fetch courses: {e}[/red]")
            return
    
    if not courses:
        console.print("[yellow]No courses with video found.[/yellow]")
        return
    
    # If course_id specified, find it
    selected_course = None
    if course_id:
        for course in courses:
            if course['id'] == course_id:
                selected_course = course
                break
        if not selected_course:
            console.print(f"[red]Course ID {course_id} not found.[/red]")
            return
    
    # Main interaction loop
    while True:
        if not selected_course:
            console.print()
            display_courses(courses)
            console.print()
            
            choice = Prompt.ask(
                "Enter course number (or 'q' to quit)",
                default="q"
            )
            
            if choice.lower() == 'q':
                console.print("[dim]Goodbye![/dim]")
                break
            
            try:
                course_idx = int(choice) - 1
                if 0 <= course_idx < len(courses):
                    selected_course = courses[course_idx]
                else:
                    console.print("[red]Invalid course number.[/red]")
                    continue
            except ValueError:
                console.print("[red]Please enter a valid number.[/red]")
                continue
        
        # Fetch sessions for selected course
        console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(description=f"Fetching videos for {selected_course['name'][:40]}...", total=None)
            try:
                video_url = canvas_client.get_course_video_url(selected_course['id'])
                
                # Reset video client for new course
                video_client.token = None
                video_client.token_id = None
                video_client.canvas_course_id = None
                
                video_client.login(video_url)
                
                if not video_client.token:
                    console.print("[red]Video authentication failed.[/red]")
                    selected_course = None
                    continue
                
                sessions = video_client.get_sessions()
                progress.update(task, description=f"[green]✓ Found {len(sessions)} videos[/green]")
            except Exception as e:
                console.print(f"[red]Failed to fetch videos: {e}[/red]")
                selected_course = None
                continue
        
        console.print()
        session_list = display_sessions(sessions, selected_course['name'])
        
        if not session_list:
            selected_course = None
            continue
        
        console.print()
        video_choice = Prompt.ask(
            "Enter video number for details (or 'b' to go back, 'q' to quit)",
            default="b"
        )
        
        if video_choice.lower() == 'q':
            console.print("[dim]Goodbye![/dim]")
            break
        elif video_choice.lower() == 'b':
            selected_course = None
            continue
        
        try:
            video_idx = int(video_choice) - 1
            if 0 <= video_idx < len(session_list):
                session = session_list[video_idx]
                video_id = session.get('id') or session.get('videoId')
                
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    task = progress.add_task(description="Fetching video info...", total=None)
                    try:
                        video_info = video_client.get_video_info(video_id=video_id)
                        if video_info:
                            urls = video_client.get_video_urls(video_info)
                            progress.update(task, description="[green]✓ Got video info[/green]")
                        else:
                            console.print("[red]Failed to get video info.[/red]")
                            continue
                    except Exception as e:
                        console.print(f"[red]Error: {e}[/red]")
                        continue
                
                console.print()
                display_video_channels(video_info, urls)
                
                console.print()
                Prompt.ask("Press Enter to continue", default="")
            else:
                console.print("[red]Invalid video number.[/red]")
        except ValueError:
            console.print("[red]Please enter a valid number.[/red]")


if __name__ == '__main__':
    main()
