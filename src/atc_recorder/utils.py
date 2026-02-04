"""Shared utilities for ATC Recorder."""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def parse_duration(duration_str: str) -> int:
    """Parse a duration string like '30m', '2h', '1h30m' into seconds.
    
    Args:
        duration_str: Duration string (e.g., '30m', '2h', '1h30m', '90s')
        
    Returns:
        Duration in seconds
        
    Raises:
        ValueError: If the duration string is invalid
    """
    if not duration_str:
        raise ValueError("Duration string cannot be empty")
    
    # If it's just a number, assume seconds
    if duration_str.isdigit():
        return int(duration_str)
    
    total_seconds = 0
    pattern = r'(\d+)([hms])'
    matches = re.findall(pattern, duration_str.lower())
    
    if not matches:
        raise ValueError(f"Invalid duration format: {duration_str}")
    
    for value, unit in matches:
        value = int(value)
        if unit == 'h':
            total_seconds += value * 3600
        elif unit == 'm':
            total_seconds += value * 60
        elif unit == 's':
            total_seconds += value
    
    return total_seconds


def format_duration(seconds: int) -> str:
    """Format seconds into a human-readable duration string.
    
    Args:
        seconds: Duration in seconds
        
    Returns:
        Formatted string like '2h 30m'
    """
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    
    return " ".join(parts)


def get_timestamp_string(dt: Optional[datetime] = None) -> str:
    """Get a timestamp string in the format used by LiveATC.
    
    Args:
        dt: Datetime object (defaults to current UTC time)
        
    Returns:
        Timestamp string like '1200Z'
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%H%MZ")


def get_date_string(dt: Optional[datetime] = None) -> str:
    """Get a date string in ISO format.
    
    Args:
        dt: Datetime object (defaults to current UTC time)
        
    Returns:
        Date string like '2026-02-03'
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def get_liveatc_date_string(dt: Optional[datetime] = None) -> str:
    """Get a date string in LiveATC archive format.
    
    Args:
        dt: Datetime object (defaults to current UTC time)
        
    Returns:
        Date string like 'Feb-03-2026'
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%b-%d-%Y")


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary.
    
    Args:
        path: Path to the directory
        
    Returns:
        The path object
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_ffmpeg() -> bool:
    """Check if ffmpeg is available on the system.
    
    Returns:
        True if ffmpeg is available, False otherwise
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be safe for use as a filename.
    
    Args:
        name: The string to sanitize
        
    Returns:
        Sanitized filename
    """
    # Replace problematic characters with underscores
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip('. ')
    return sanitized
