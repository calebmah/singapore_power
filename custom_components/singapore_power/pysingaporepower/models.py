"""Data models for the pysingaporepower library."""

from dataclasses import dataclass
from datetime import datetime

@dataclass
class Premise:
    """Represents a single premise with utility accounts."""
    account_no: str
    premise_no: str
    active_services: list[str]

@dataclass
class Consumption:
    """Represents a single consumption data point."""
    date: datetime
    value: float
