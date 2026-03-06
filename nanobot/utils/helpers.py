"""Utility functions for nanobot."""

import os
from datetime import datetime
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def expand_path(path: str | Path) -> Path:
    """Expand environment variables and user home in a path."""
    return Path(os.path.expandvars(str(path))).expanduser()


def get_nanobot_home_path() -> Path:
    """Get the nanobot data root, overridable with NANOBOT_HOME."""
    configured = os.getenv("NANOBOT_HOME")
    if configured:
        return expand_path(configured)

    legacy_home = Path.home() / ".nanobot"
    migrated_home = Path.home() / "run" / ".nanobot"

    # Migration fallback: prefer the new location when it has a config file
    # and the legacy directory does not.
    if not (legacy_home / "config.json").exists() and (migrated_home / "config.json").exists():
        return migrated_home

    return legacy_home


def get_data_path() -> Path:
    """Get the nanobot data directory."""
    return ensure_dir(get_nanobot_home_path())


def get_workspace_path(workspace: str | None = None) -> Path:
    """
    Get the workspace path.
    
    Args:
        workspace: Optional workspace path. Defaults to <nanobot_home>/workspace.
    
    Returns:
        Expanded and ensured workspace path.
    """
    if workspace:
        path = expand_path(workspace)
    else:
        path = get_nanobot_home_path() / "workspace"
    return ensure_dir(path)


def get_sessions_path() -> Path:
    """Get the sessions storage directory."""
    return ensure_dir(get_data_path() / "sessions")


def get_media_path(media_dir: str | None = None) -> Path:
    """Get the media storage directory."""
    if media_dir:
        return ensure_dir(expand_path(media_dir))
    return ensure_dir(get_data_path() / "media")


def get_bridge_path() -> Path:
    """Get the bridge directory."""
    return ensure_dir(get_data_path() / "bridge")


def get_memory_path(workspace: Path | None = None) -> Path:
    """Get the memory directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "memory")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def today_date() -> str:
    """Get today's date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    # Replace unsafe characters
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """
    Parse a session key into channel and chat_id.
    
    Args:
        key: Session key in format "channel:chat_id"
    
    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]
