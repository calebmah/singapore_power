"""Redacted Diagnostics support for the Singapore Power (SP Group) integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ELECTRICITY_PRICE,
    CONF_ENABLE_EXTENDED_INSIGHTS,
    CONF_ENABLE_RECORDER_STATS,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_ENABLE_EXTENDED_INSIGHTS,
    DEFAULT_ENABLE_RECORDER_STATS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    parse_electricity_price,
)
from .coordinator import SingaporePowerDataUpdateCoordinator
from .pysingaporepower import AccountBillingSummary, OptionalEcosystemData, Premise


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return PII-redacted diagnostics for a Singapore Power config entry."""
    coordinator: SingaporePowerDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    ) or hass.data.get(DOMAIN, {}).get(entry.entry_id)

    entry_diag = {
        "version": entry.version,
        "has_access_token": bool(entry.data.get(CONF_ACCESS_TOKEN)),
        "has_id_token": bool(entry.data.get(CONF_ID_TOKEN)),
        "has_refresh_token": bool(entry.data.get(CONF_REFRESH_TOKEN)),
        "has_stored_password": CONF_PASSWORD in entry.data,
        "options": {
            CONF_SCAN_INTERVAL_MINUTES: entry.options.get(
                CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES
            ),
            CONF_ENABLE_RECORDER_STATS: entry.options.get(
                CONF_ENABLE_RECORDER_STATS, DEFAULT_ENABLE_RECORDER_STATS
            ),
            CONF_ENABLE_EXTENDED_INSIGHTS: entry.options.get(
                CONF_ENABLE_EXTENDED_INSIGHTS, DEFAULT_ENABLE_EXTENDED_INSIGHTS
            ),
            "has_custom_electricity_price": (
                parse_electricity_price(entry.options.get(CONF_ELECTRICITY_PRICE))
                is not None
            ),
        },
    }

    if coordinator is None:
        return {"entry": entry_diag, "coordinator": None}

    data = coordinator.data or {}
    premises_diag: list[dict[str, Any]] = []
    idx = 0
    for key, pdata in data.items():
        if key.startswith("_") or not isinstance(pdata, dict):
            continue
        idx += 1
        premise = pdata.get("premise")
        billing = pdata.get("billing")
        utils = pdata.get("utilities") or {}
        fcus = pdata.get("fcus") or []

        premises_diag.append(
            {
                "premise_index": idx,
                "premise_type": premise.premise_type if isinstance(premise, Premise) else None,
                "contestable_type": (
                    premise.contestable_type if isinstance(premise, Premise) else None
                ),
                "ppms": premise.ppms if isinstance(premise, Premise) else False,
                "is_owner": premise.is_owner if isinstance(premise, Premise) else None,
                "active_services": list(pdata.get("active_services") or []),
                "ami_utilities": (
                    list(premise.ami_utilities) if isinstance(premise, Premise) else []
                ),
                "utilities_with_ami_data": [
                    uk
                    for uk, uval in utils.items()
                    if isinstance(uval, dict) and uval.get("has_ami")
                ],
                "has_billing": isinstance(billing, AccountBillingSummary),
                "bills_count": (
                    len(billing.bills) if isinstance(billing, AccountBillingSummary) else 0
                ),
                "meter_readings_utilities": sorted(
                    list((pdata.get("meter_readings") or {}).keys())
                ),
                "green_goals_utilities": sorted(
                    list((pdata.get("green_goals") or {}).keys())
                ),
                "has_tariff": pdata.get("tariff") is not None,
                "paired_fcus_count": len(fcus) if isinstance(fcus, list) else 0,
                "anomalies_utilities": sorted(
                    list((pdata.get("anomalies") or {}).keys())
                ),
            }
        )

    acct_meta = data.get("_account") or {}
    ecosystem = acct_meta.get("ecosystem")
    ecosystem_diag = (
        {
            "has_greenup": ecosystem.greenup is not None,
            "has_ev_wallet": ecosystem.ev_wallet is not None,
            "has_ev_session": ecosystem.ev_session is not None,
            "has_ev_last_charge": ecosystem.ev_last_charge is not None,
            "has_ev_unpaid": ecosystem.ev_unpaid is not None,
            "has_unread_notifications": ecosystem.unread_notifications is not None,
        }
        if isinstance(ecosystem, OptionalEcosystemData)
        else {}
    )

    return {
        "entry": entry_diag,
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_success": (
                coordinator.last_success.isoformat()
                if coordinator.last_success
                else None
            ),
            "last_poll_ms": coordinator.last_poll_ms,
            "last_error": coordinator.last_error,
            "premise_count": len(premises_diag),
            "failed_optional_reads": coordinator.api.read_failures,
        },
        "premises": premises_diag,
        "ecosystem": ecosystem_diag,
    }
