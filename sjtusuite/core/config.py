"""Centralized configuration loading for SJTU Suite."""
from pathlib import Path


def get_project_root():
    """Get the project root directory."""
    return Path(__file__).parent.parent.parent


def get_data_dir():
    """Get the data directory, creating it if needed."""
    data_dir = get_project_root() / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


def get_sessions_dir():
    """Get the sessions directory for cookie files."""
    sessions_dir = get_data_dir() / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    return sessions_dir




def get_password_file():
    """Get password from password.txt for testing."""
    pw_path = get_project_root() / "password.txt"
    if pw_path.exists():
        with open(pw_path, "r") as f:
            username = f.readline().strip()
            password = f.readline().strip()
        return username, password
    return None, None
