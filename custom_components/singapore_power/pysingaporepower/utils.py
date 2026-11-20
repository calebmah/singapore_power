"""Utilities for the pysingaporepower library."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
import re
from typing import TYPE_CHECKING, Any
import unicodedata
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .const import (
    CENTS_PER_DOLLAR,
    ERROR_VALUE_CHARS,
    EVA_INTEGER_CENTS_MIN,
    KNOWN_ROUTE_SEGMENTS,
    MAX_USERNAME_OCTETS,
)

if TYPE_CHECKING:
    from .models import Consumption

SG_TZ = ZoneInfo("Asia/Singapore")

MAX_MONEY = Decimal("1e12")
MONEY_PRECISION = 20

_FCU_KEY_SAFE = re.compile(r"[^0-9A-Za-z]+")
_FCU_KEY_DIGEST_CHARS = 8

_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y%m%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m",
    "%b %Y",
)


def fold_text(value: str) -> str:
    """NFKC-normalized, casefolded, stripped comparison form for server/user text."""
    return unicodedata.normalize("NFKC", value).casefold().strip()


def clean_log_text(value: object) -> str:
    """Remove control and bidi-override characters before logging or UI display."""
    return " ".join("".join(ch for ch in str(value) if ch.isprintable()).split())


def safe_log_text(value: object, limit: int = ERROR_VALUE_CHARS) -> str:
    """Clean and length-cap untrusted remote text for logs and error dialogs."""
    cleaned = clean_log_text(value)
    return cleaned if len(cleaned) <= limit else f"{cleaned[:limit]}..."


def validate_username(raw: str) -> str:
    """Validate an e-account username/email within RFC 5321 UTF-8 octet limits."""
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Username must not be empty")
    try:
        octets = len(cleaned.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("Username is not valid Unicode") from exc
    if octets > MAX_USERNAME_OCTETS:
        raise ValueError(f"Username must be at most {MAX_USERNAME_OCTETS} bytes")
    return cleaned


def quote_path_segment(segment: str) -> str:
    """URL-escape a dynamic path segment (premise_id, account_no) so '/' or '?' cannot traverse."""
    return quote(str(segment).strip(), safe="")


def redact_url_for_log(url: str) -> str:
    """Strip query strings and replace dynamic account/premise/order segments with '{id}'."""
    without_query, _, _ = url.partition("?")
    scheme, sep, rest = without_query.partition("://")
    host, _, path = rest.partition("/")
    segments = [
        seg if seg in KNOWN_ROUTE_SEGMENTS else "{id}"
        for seg in path.split("/")
        if seg
    ]
    root = f"{scheme}{sep}{host}" if sep else host
    return "/".join([root, *segments]) if segments else root


def fcu_sensor_key(thing_name: str) -> str:
    """Collision-safe entity key suffix for a Frosty Tengah Fan Coil Unit."""
    folded = fold_text(thing_name)
    safe = _FCU_KEY_SAFE.sub("_", folded).strip("_")
    if safe and folded.isascii():
        return f"fcu_{safe}"
    digest = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:_FCU_KEY_DIGEST_CHARS]
    return f"fcu_{safe}_{digest}" if safe else f"fcu_{digest}"


def now_sg() -> datetime:
    """Return current timezone-aware datetime in Asia/Singapore."""
    return datetime.now(SG_TZ)


def to_sg_datetime(dt: datetime) -> datetime:
    """Convert any datetime (aware or naive) to Asia/Singapore."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SG_TZ)
    return dt.astimezone(SG_TZ)


def start_of_sg_day(d: date | datetime) -> datetime:
    """Return 00:00:00 Asia/Singapore for the given date or datetime."""
    if isinstance(d, datetime):
        d = to_sg_datetime(d).date()
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=SG_TZ)


def end_of_sg_day(d: date | datetime) -> datetime:
    """Return 23:59:59 Asia/Singapore for the given date or datetime."""
    if isinstance(d, datetime):
        d = to_sg_datetime(d).date()
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=SG_TZ)


def start_of_sg_month(d: date | datetime) -> datetime:
    """Return 00:00:00 on the 1st of the month in Asia/Singapore."""
    if isinstance(d, datetime):
        d = to_sg_datetime(d).date()
    return datetime(d.year, d.month, 1, 0, 0, 0, tzinfo=SG_TZ)


def months_ago_sg_start(d: date | datetime, months_count: int) -> datetime:
    """Return 00:00:00 SGT on the 1st of the month (months_count - 1) months prior to d."""
    if isinstance(d, datetime):
        d = to_sg_datetime(d).date()
    month = d.month - (months_count - 1)
    year = d.year
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1, 0, 0, 0, tzinfo=SG_TZ)


def format_api_date(dt: datetime) -> str:
    """Format a datetime into YYYYMMDDHHMMSS in Asia/Singapore as required by SP Jarvis/Frosty APIs."""
    return to_sg_datetime(dt).strftime("%Y%m%d%H%M%S")


def parse_sp_datetime(value: str | None) -> datetime | None:
    """Parse timestamps returned by SP APIs into Asia/Singapore datetimes, guarding UTC/SGT bounds."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    normalized = raw
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", normalized):
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        pass

    if parsed is None and len(raw) == 14 and raw.isdigit():
        try:
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
        except ValueError:
            pass

    if parsed is None:
        for fmt in _DATE_PATTERNS:
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SG_TZ)

    # Guard against near-min/max timestamps ('0001-01-01T00:00:00') that overflow when shifted to UTC
    try:
        sg_dt = parsed.astimezone(SG_TZ)
        sg_dt.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        return None

    return sg_dt


def parse_sp_date(value: str | None) -> date | None:
    """Parse a date or datetime string returned by SP APIs into a Python date."""
    dt = parse_sp_datetime(value)
    return dt.date() if dt is not None else None


def safe_float(value: Any, default: float | None = None) -> float | None:
    """Safely convert strings/numbers to finite float (rejecting bool, NaN, and Infinity)."""
    if value is None or value == "" or isinstance(value, bool):
        return default
    if not isinstance(value, (int, float, str)):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def round_half_up(value: Decimal, exponent: str = "0.01") -> float | None:
    """Round a Decimal half-up within MAX_MONEY bounds."""
    if not value.is_finite() or abs(value) > MAX_MONEY:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = MONEY_PRECISION
            return float(value.quantize(Decimal(exponent), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def round_sgd(value: float | str | Decimal | None) -> float | None:
    """Round an SGD dollar amount to 2 decimal places using Decimal ROUND_HALF_UP."""
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        return None
    return round_half_up(dec, "0.01")


def cents_to_dollars(value: Any) -> float | None:
    """Convert integer cents (Njord Bill/Payable/Transaction & GreenGoal) to SGD half-up."""
    num = safe_float(value)
    if num is None:
        return None
    try:
        dec = Decimal(str(num)) / Decimal(str(CENTS_PER_DOLLAR))
    except InvalidOperation:
        return None
    return round_half_up(dec, "0.01")


def parse_eva_sgd(value: Any) -> float | None:
    """Parse Eva EV string/integer money fields (dotted = dollars; integer >= 100 = cents)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return round_sgd(value) if math.isfinite(value) else None
    if isinstance(value, int):
        dec = Decimal(value)
        return round_half_up(dec / 100 if abs(value) >= EVA_INTEGER_CENTS_MIN else dec, "0.01")
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if "." not in text:
            whole = safe_float(text)
            if whole is not None and whole.is_integer():
                ival = int(whole)
                dec = Decimal(ival)
                return round_half_up(
                    dec / 100 if abs(ival) >= EVA_INTEGER_CENTS_MIN else dec, "0.01"
                )
        num = safe_float(text)
        return round_sgd(num) if num is not None else None
    return None


def normalize_account_no(account_no: str | None) -> str:
    """Normalize SP account numbers (strip leading '00' on >=12-digit SAP/EBS numbers)."""
    if not account_no:
        return ""
    cleaned = fold_text(str(account_no))
    if len(cleaned) >= 12 and cleaned.startswith("00"):
        return cleaned[2:]
    return cleaned


def account_digits(account_no: str | None) -> str:
    """Canonical digit string with all leading zeros stripped for cross-service matching."""
    if not account_no:
        return ""
    stripped = fold_text(str(account_no)).lstrip("0")
    return stripped or "0"


def normalize_unit(unit: str | None, fallback: str = "") -> str:
    """Normalize SP unit strings ('Cu M', 'kwh', etc.) to canonical Home Assistant units."""
    if not unit:
        return fallback
    folded = fold_text(unit)
    compact = "".join(folded.split())
    if compact in ("cum", "cu.m", "cbm", "m3", "m³", "cubicmeter", "cubicmeters", "cubicmetre"):
        return "m³"
    if compact in ("kwh", "kw·h", "kwhr"):
        return "kWh"
    return unit.strip() or fallback


def decode_jwt_exp(token: str | None) -> int | None:
    """Extract the 'exp' UNIX timestamp claim from a JWT without external dependencies."""
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        decoded = base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8")
        claims = json.loads(decoded)
        exp = claims.get("exp") if isinstance(claims, dict) else None
        return int(exp) if exp is not None else None
    except Exception:
        return None


def drop_future_points(points: list[Consumption], now: datetime) -> list[Consumption]:
    """Drop padded AMI slots that start at or after 'now'."""
    return [pt for pt in points if pt.date < now]


def trim_unreported_points(points: list[Consumption]) -> list[Consumption]:
    """Drop trailing zero-consumption slots that SP's AMI head-end has not populated yet."""
    ordered = sorted(points, key=lambda p: p.date)
    last_nonzero = -1
    for idx, pt in enumerate(ordered):
        if pt.value > 0:
            last_nonzero = idx
    if last_nonzero < 0:
        return []
    return ordered[: last_nonzero + 1]


def merge_ami_daily_and_half_hourly(
    daily_points: list[Consumption],
    half_hourly_points: list[Consumption],
) -> list[Consumption]:
    """Prefer half-hourly AMI slots on days they cover; retain daily points for older months."""
    trimmed_hh = trim_unreported_points(half_hourly_points)
    hh_days = {pt.date.astimezone(SG_TZ).date() for pt in trimmed_hh}
    merged = [
        pt for pt in daily_points if pt.date.astimezone(SG_TZ).date() not in hh_days
    ]
    merged.extend(trimmed_hh)
    merged.sort(key=lambda p: p.date)
    return merged
