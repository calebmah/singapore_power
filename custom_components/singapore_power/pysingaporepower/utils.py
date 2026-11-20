"""Utilities for the pysingaporepower library."""

from datetime import datetime

def format_api_date(dt: datetime) -> str:
    """Formats a datetime object into the string format required by the API."""
    return dt.strftime("%Y%m%d%H%M%S")
