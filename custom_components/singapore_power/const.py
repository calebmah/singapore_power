"""Constants and option helpers for the Singapore Power Home Assistant integration."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.const import Platform

from .pysingaporepower.utils import safe_log_text

DOMAIN = "singapore_power"
ATTRIBUTION = "Data provided by SP Group"
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Stored credential & token keys in ConfigEntry.data (no password stored at rest)
CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRY = "token_expiry"

# Options keys in ConfigEntry.options
CONF_SCAN_INTERVAL_MINUTES = "scan_interval_minutes"
CONF_ENABLE_RECORDER_STATS = "enable_recorder_statistics"
CONF_ENABLE_EXTENDED_INSIGHTS = "enable_extended_insights"
CONF_ELECTRICITY_PRICE = "electricity_price"

# Defaults & bounds
DEFAULT_SCAN_INTERVAL_MINUTES = 30
MIN_SCAN_INTERVAL_MINUTES = 15
MAX_SCAN_INTERVAL_MINUTES = 360
DEFAULT_ENABLE_RECORDER_STATS = True
DEFAULT_ENABLE_EXTENDED_INSIGHTS = True
MAX_ELECTRICITY_PRICE_SGD_KWH = 10.0

# Metadata refresh interval for slow-changing endpoints (tariffs, GreenUP, Green Goals, premise types)
METADATA_REFRESH_HOURS = 6
SLOW_POLL_MS = 60_000
LEDGER_STORAGE_VERSION = 2


def ledger_storage_key(entry_id: str) -> str:
    """Return the Home Assistant Store key for a Singapore Power config entry ledger."""
    return f"{DOMAIN}_ledger_{entry_id}"


def parse_electricity_price(raw: object) -> float | None:
    """Return validated custom electricity tariff (SGD/kWh) from options, or None if unset."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    if not isinstance(raw, (int, float, str)):
        return None
    text = raw.strip() if isinstance(raw, str) else raw
    if text == "":
        return None
    try:
        val = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(val) or val <= 0.0 or val > MAX_ELECTRICITY_PRICE_SGD_KWH:
        return None
    return round(val, 6)


def parse_electricity_price_input(raw: object) -> float | None:
    """Validate user input for electricity_price in OptionsFlow ('' or 0 clears override)."""
    if raw is None or isinstance(raw, bool):
        if isinstance(raw, bool):
            raise ValueError("Invalid electricity price")
        return None
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned:
            return None
        val = float(cleaned)
    elif isinstance(raw, (int, float)):
        val = float(raw)
    else:
        raise ValueError("Invalid electricity price")

    if not math.isfinite(val):
        raise ValueError("Electricity price must be finite")
    if val == 0.0:
        return None
    if val < 0.0 or val > MAX_ELECTRICITY_PRICE_SGD_KWH:
        raise ValueError(
            f"Electricity price must be between 0 and {MAX_ELECTRICITY_PRICE_SGD_KWH} SGD/kWh"
        )
    return round(val, 6)


def translated_error(key: str, err: object) -> dict[str, Any]:
    """Build Home Assistant exception translation kwargs with sanitized error text."""
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": {"error": safe_log_text(err)},
    }
