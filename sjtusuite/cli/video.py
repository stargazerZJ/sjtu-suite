"""
SJTU Video CLI - Terminal UI for browsing and downloading course video recordings.

Provides an interactive interface to:
- View enrolled courses from Canvas
- List video sessions for each course
- Download multiple videos with progress bars (parallel downloads supported)
"""

import os
import click
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)
from rich.columns import Columns
from rich.text import Text

from sjtusuite.auth import JACLogin
from sjtusuite.clients.canvas import CanvasClient
from sjtusuite.clients.video import VideoClient
from sjtusuite.core.credentials import credentials


console = Console()


def create_clients():
    """Initialize and return authenticated clients."""
    jac_login = JACLogin(credentials.username, credentials.password)
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
    """Display video sessions in a compact multi-column layout grouped by date."""
    if not sessions:
        console.print("[yellow]No video sessions found for this course.[/yellow]")
        return []
    
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
        
        # Build entries for this date
        entries = []
        for s in groups[date]:
            video_name = s.get('videoName') or s.get('name') or 'Unnamed Video'
            # Truncate long names for compact display
            if len(video_name) > 40:
                video_name = video_name[:37] + "..."
            entry = Text()
            entry.append(f"{idx:3}. ", style="dim")
            entry.append(video_name, style="cyan")
            entries.append(entry)
            session_list.append(s)
            idx += 1
        
        # Display in columns (2-3 columns depending on terminal width)
        term_width = console.width
        if term_width >= 120:
            num_cols = 3
        elif term_width >= 80:
            num_cols = 2
        else:
            num_cols = 1
        
        if num_cols > 1:
            console.print(Columns(entries, equal=True, expand=True))
        else:
            for entry in entries:
                console.print(f"  {entry}")
    
    return session_list


def display_video_channels(video_info, channels):
    """Display download channels for a video in a compact format."""
    video_name = video_info.get('videName', 'Unknown Video')
    
    console.print(Panel(f"[bold]{video_name}[/bold]", title="🎬 Video Details", style="green"))
    
    if not channels:
        console.print("[yellow]No download channels available.[/yellow]")
        return
    
    # Use columns for compact display when multiple channels
    if len(channels) <= 4:
        entries = []
        for idx, (channel, url, desc) in enumerate(channels, 1):
            entry = Text()
            entry.append(f"[{idx}] ", style="bold cyan")
            entry.append(desc, style="green")
            entries.append(entry)
        console.print(Columns(entries, equal=True, expand=True))
    else:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("#", style="dim", width=4)
        table.add_column("Channel", style="cyan")
        for idx, (channel, url, desc) in enumerate(channels, 1):
            table.add_row(str(idx), desc)
        console.print(table)


def display_channel_options(channel_names):
    """Display available channel options for batch download."""
    console.print("\n[bold cyan]Available channels:[/bold cyan]")
    entries = []
    for idx, name in enumerate(channel_names, 1):
        entry = Text()
        entry.append(f"[{idx}] ", style="bold cyan")
        entry.append(name, style="green")
        entries.append(entry)
    console.print(Columns(entries, equal=True, expand=True))


def parse_selection(selection_str, max_idx):
    """
    Parse a selection string like "1,3,5-7" into a list of indices.
    
    Args:
        selection_str: String with comma-separated numbers or ranges (e.g., "1,3,5-7")
        max_idx: Maximum valid index
        
    Returns:
        list: List of valid indices (1-based)
    """
    indices = []
    parts = selection_str.replace(' ', '').split(',')
    
    for part in parts:
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-', 1)
                start = int(start)
                end = int(end)
                for i in range(start, min(end + 1, max_idx + 1)):
                    if i >= 1 and i not in indices:
                        indices.append(i)
            except ValueError:
                continue
        else:
            try:
                idx = int(part)
                if 1 <= idx <= max_idx and idx not in indices:
                    indices.append(idx)
            except ValueError:
                continue
    
    return sorted(indices)


def _download_single_video(video_client, session, output_dir, channel_idx, progress, task_id, task_num, total):
    """
    Download a single video file with progress updates.
    
    Args:
        video_client: VideoClient instance
        session: Session dict
        output_dir: Output directory
        channel_idx: Channel index to download
        progress: Rich Progress instance
        task_id: Task ID for progress updates
        task_num: Current task number (for display)
        total: Total number of tasks
        
    Returns:
        tuple: (output_path, success)
    """
    video_name = session.get('videoName') or session.get('name') or f'video_{task_num}'
    # Sanitize filename
    safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_', '.') else '_' for c in video_name)
    safe_name = safe_name.strip()[:100]
    
    try:
        video_id = session.get('id') or session.get('videoId')
        video_info = video_client.get_video_info(video_id=video_id)
        
        if not video_info:
            progress.update(task_id, description=f"[red]✗[/red] ({task_num}/{total}) {safe_name[:35]} - No info")
            return (None, False)
        
        urls = video_client.get_video_urls(video_info)
        
        if not urls:
            progress.update(task_id, description=f"[red]✗[/red] ({task_num}/{total}) {safe_name[:35]} - No URLs")
            return (None, False)
        
        # Select channel
        if channel_idx < len(urls):
            channel, url, desc = urls[channel_idx]
        else:
            channel, url, desc = urls[0]
        
        # Filename
        if len(urls) > 1:
            filename = f"{safe_name}_ch{channel}.mp4"
        else:
            filename = f"{safe_name}.mp4"
        
        output_path = os.path.join(output_dir, filename)
        
        # Update task description
        progress.update(task_id, description=f"[cyan]({task_num}/{total})[/cyan] {safe_name[:40]}")
        
        # Download headers
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://v.sjtu.edu.cn/",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-site",
        }
        
        response = video_client.session.get(url, stream=True, headers=headers)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        
        if total_size > 0:
            progress.update(task_id, total=total_size)
        
        downloaded = 0
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    progress.update(task_id, completed=downloaded)
        
        progress.update(task_id, description=f"[green]✓[/green] ({task_num}/{total}) {safe_name[:40]}")
        return (output_path, True)
        
    except Exception as e:
        progress.update(task_id, description=f"[red]✗[/red] ({task_num}/{total}) {safe_name[:35]} - {str(e)[:15]}")
        return (None, False)


def download_videos_parallel(video_client, sessions, output_dir="./videos", channel_idx=0, max_parallel=3):
    """
    Download multiple videos with progress bars, supporting parallel downloads.
    
    Args:
        video_client: Authenticated VideoClient instance
        sessions: List of session dicts to download
        output_dir: Directory to save videos
        channel_idx: Which channel to download (0 = main camera)
        max_parallel: Maximum number of parallel downloads
    """
    if not sessions:
        console.print("[yellow]No videos to download.[/yellow]")
        return
    
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    total = len(sessions)
    console.print(f"\n[bold cyan]Downloading {total} video(s) to:[/bold cyan] {output_dir}")
    console.print(f"[dim]Parallel downloads: {min(max_parallel, total)}[/dim]\n")
    
    results = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True,
    ) as progress:
        
        if max_parallel <= 1:
            # Sequential download
            for i, session in enumerate(sessions, 1):
                task_id = progress.add_task(f"[cyan]({i}/{total})[/cyan] Starting...", total=None)
                result = _download_single_video(
                    video_client, session, output_dir, channel_idx,
                    progress, task_id, i, total
                )
                results.append(result)
        else:
            # Parallel download
            task_ids = {}
            futures = {}
            
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                # Submit all tasks
                for i, session in enumerate(sessions, 1):
                    task_id = progress.add_task(f"[dim]({i}/{total})[/dim] Queued...", total=None)
                    future = executor.submit(
                        _download_single_video,
                        video_client, session, output_dir, channel_idx,
                        progress, task_id, i, total
                    )
                    futures[future] = (i, task_id)
                
                # Collect results as they complete
                for future in as_completed(futures):
                    task_num, task_id = futures[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        progress.update(task_id, description=f"[red]✗[/red] ({task_num}/{total}) Error: {str(e)[:30]}")
                        results.append((None, False))
    
    # Summary
    success_count = sum(1 for _, success in results if success)
    console.print(f"\n[green]Download complete![/green] {success_count}/{total} videos saved to: {output_dir}")


def get_common_channels(video_client, sessions):
    """
    Get channel info from the first video to determine available channels.
    
    Returns:
        list: List of (channel_idx, name) tuples, or None if failed
    """
    if not sessions:
        return None
    
    first_session = sessions[0]
    video_id = first_session.get('id') or first_session.get('videoId')
    
    try:
        video_info = video_client.get_video_info(video_id=video_id)
        if not video_info:
            return None
        
        urls = video_client.get_video_urls(video_info)
        if not urls:
            return None
        
        return [(i, desc) for i, (ch, url, desc) in enumerate(urls)]
    except:
        return None


def download_subtitles(video_client, output_dir, course_name, sessions=None):
    """
    Download subtitles for the current course or specific videos as SRT file(s).
    
    Args:
        video_client: Authenticated VideoClient instance
        output_dir: Directory to save subtitles
        course_name: Name of the course for filename
        sessions: List of specific session dicts to download subtitles for.
    """
    console.print("\n[cyan]Fetching subtitles...[/cyan]")
    
    # Check if we have course ID
    if not video_client.video_course_id:
        console.print("[yellow]No video course ID available. This course may not have subtitle support.[/yellow]")
        console.print(f"[dim]Debug: canvas_course_id = {video_client.canvas_course_id}[/dim]")
        return False
    
    
    os.makedirs(output_dir, exist_ok=True)
    
    if sessions:
        # Download subtitles for specific videos
        for session in sessions:
            video_id = session.get('courId')
            console.print(f"Downloading subtitles for video {video_id}")
            video_name = session.get('videoName') or session.get('name') or f'video_{video_id}'
            
            # Filter subtitles for this video
            all_subtitles = video_client.get_subtitles(course_id=video_id)
            if not all_subtitles:
                console.print(f"[yellow]No subtitles for: {video_name}[/yellow]")
                continue
            
            srt_content = video_client.format_subtitles_srt(all_subtitles)
            if not srt_content:
                continue
            
            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in video_name)
            safe_name = safe_name.strip()[:80]
            
            output_path = os.path.join(output_dir, f"{safe_name}.srt")
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(srt_content)
            
            console.print(f"[green]✓[/green] {video_name}: {len(all_subtitles)} entries")
        
        return True
    else:
        console.print("[yellow]No videos to download.[/yellow]")
        return False


def download_summary(video_client, output_dir, course_name, sessions=None):
    """
    Download AI-generated summary for the current course or specific videos.
    
    Args:
        video_client: Authenticated VideoClient instance
        output_dir: Directory to save summary
        course_name: Name of the course for filename
        sessions: List of specific session dicts to download summaries for.
    """
    console.print("\n[cyan]Fetching summary...[/cyan]")
    
    # Check if we have course ID
    if not video_client.video_course_id:
        console.print("[yellow]No video course ID available. This course may not have summary support.[/yellow]")
        console.print(f"[dim]Debug: canvas_course_id = {video_client.canvas_course_id}[/dim]")
        return False
    
    os.makedirs(output_dir, exist_ok=True)
    
    if sessions:
        # Download summary for specific videos
        for session in sessions:
            video_id = session.get('courId')
            video_name = session.get('videoName') or session.get('name') or f'video_{video_id}'
            
            console.print(f"[dim]Fetching summary for: {video_name}[/dim]")
            
            summary_data = video_client.get_course_summary(course_id=video_id)
            if not summary_data:
                console.print(f"[yellow]No summary for: {video_name}[/yellow]")
                continue
            
            skims = summary_data.get("documentSkims", [])
            if not skims:
                console.print(f"[yellow]No summary segments for: {video_name}[/yellow]")
                continue
            
            txt_content = video_client.format_summary_txt(summary_data)
            if not txt_content:
                continue
            
            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in video_name)
            safe_name = safe_name.strip()[:80]
            
            output_path = os.path.join(output_dir, f"{safe_name}_summary.txt")
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(txt_content)
            
            console.print(f"[green]✓[/green] {video_name}: {len(skims)} sections")
        
        return True
    else:
        console.print("[yellow]No videos specified for summary download.[/yellow]")
        return False


@click.command()
@click.option('--course-id', '-c', type=int, help='Directly access a specific course by ID')
@click.option('--output-dir', '-o', type=str, default='./videos', help='Output directory for downloads')
@click.option('--parallel', '-p', type=int, default=3, help='Number of parallel downloads (1-10, default: 3)')
@click.option('--subtitles', '-s', is_flag=True, help='Download subtitles only (non-interactive)')
def main(course_id, output_dir, parallel, subtitles):
    """Interactive CLI for browsing and downloading SJTU course videos."""
    # Clamp parallel value
    parallel = max(1, min(10, parallel))
    
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
                video_client.video_course_id = None
                
                video_client.login(video_url)
                
                if not video_client.token:
                    console.print("[red]Video authentication failed.[/red]")
                    selected_course = None
                    continue
                
                sessions = video_client.get_sessions()
                progress.update(task, description=f"[green]✓ Found {len(sessions)} videos[/green]")
                
                # Debug: Show if we got the course ID
                if video_client.video_course_id:
                    console.print(f"[dim]  Video course ID: {video_client.video_course_id}[/dim]")
                elif sessions:
                    # Log first session keys to help debug
                    console.print(f"[dim]  Warning: No video_course_id found. Session keys: {list(sessions[0].keys())[:10]}[/dim]")
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
        console.print("[dim]Tip: '1,3,5-7' to download | '50s' for subtitles | '50m' for summary[/dim]")
        video_choice = Prompt.ask(
            "Enter command",
            default="b"
        )
        
        if video_choice.lower() == 'q':
            console.print("[dim]Goodbye![/dim]")
            break
        elif video_choice.lower() == 'b':
            selected_course = None
            continue
        
        # Check for per-video subtitles: e.g., "50s" or "1-5s" or "1,3,5s"
        if video_choice.endswith('s') and len(video_choice) > 1:
            sub_selection = video_choice[:-1]  # Remove trailing 's'
            sub_indices = parse_selection(sub_selection, len(session_list))
            if sub_indices:
                selected_sessions = [session_list[i - 1] for i in sub_indices]
                console.print(f"\n[cyan]Downloading subtitles for {len(selected_sessions)} video(s)...[/cyan]")
                download_subtitles(video_client, output_dir, selected_course['name'], sessions=selected_sessions)
                console.print()
                Prompt.ask("Press Enter to continue", default="")
                continue
        
        # Check for per-video summary: e.g., "50m" or "1-5m" or "1,3,5m"
        if video_choice.endswith('m') and len(video_choice) > 1:
            sum_selection = video_choice[:-1]  # Remove trailing 'm'
            sum_indices = parse_selection(sum_selection, len(session_list))
            if sum_indices:
                selected_sessions = [session_list[i - 1] for i in sum_indices]
                console.print(f"\n[cyan]Downloading summary for {len(selected_sessions)} video(s)...[/cyan]")
                download_summary(video_client, output_dir, selected_course['name'], sessions=selected_sessions)
                console.print()
                Prompt.ask("Press Enter to continue", default="")
                continue
        
        # Parse selection for video download
        selected_indices = parse_selection(video_choice, len(session_list))
        
        if not selected_indices:
            console.print("[red]No valid video numbers specified.[/red]")
            continue
        
        # Get selected sessions
        selected_sessions = [session_list[i - 1] for i in selected_indices]
        
        console.print(f"\n[cyan]Selected {len(selected_sessions)} video(s) for download.[/cyan]")
        
        # Get channel options from first video
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(description="Fetching channel info...", total=None)
            channel_info = get_common_channels(video_client, selected_sessions)
            progress.update(task, description="[green]✓ Got channel info[/green]")
        
        channel_idx = 0
        if channel_info and len(channel_info) > 1:
            # Show channel options
            display_channel_options([name for idx, name in channel_info])
            
            console.print()
            channel_choice = Prompt.ask(
                "Select channel to download (Enter for main camera)",
                default="1"
            )
            
            try:
                ch_idx = int(channel_choice) - 1
                if 0 <= ch_idx < len(channel_info):
                    channel_idx = ch_idx
            except ValueError:
                pass
        
        # Confirm and download
        console.print()
        confirm = Prompt.ask("Proceed with download?", choices=["y", "n"], default="y")
        
        if confirm.lower() == 'y':
            download_videos_parallel(
                video_client, selected_sessions, output_dir,
                channel_idx=channel_idx, max_parallel=parallel
            )
        
        console.print()
        Prompt.ask("Press Enter to continue", default="")


if __name__ == '__main__':
    main()
