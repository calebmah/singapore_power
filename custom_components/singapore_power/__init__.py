"""The Singapore Power (SP Group) integration."""

from __future__ import annotations

import logging
from typing import Any, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ELECTRICITY_PRICE,
    CONF_ENABLE_EXTENDED_INSIGHTS,
    CONF_ENABLE_RECORDER_STATS,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
    PLATFORMS,
    parse_electricity_price,
    translated_error,
)
from .coordinator import SingaporePowerDataUpdateCoordinator
from .pysingaporepower import (
    ApiException,
    AuthException,
    SingaporePowerApi,
    SingaporePowerAuth,
)
from .pysingaporepower.const import OAUTH_ERROR_REQUIRES_VERIFICATION

_LOGGER = logging.getLogger(__name__)

SingaporePowerConfigEntry: TypeAlias = ConfigEntry[SingaporePowerDataUpdateCoordinator]


def _forget_password(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip any legacy plaintext password from ConfigEntry.data before registering listeners."""
    if CONF_PASSWORD not in entry.data:
        return
    cleaned = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
    hass.config_entries.async_update_entry(entry, data=cleaned)
    _LOGGER.info("Removed legacy stored password from Singapore Power config entry")


def _non_price_options(options: dict[str, Any]) -> dict[str, Any]:
    """Return options snapshot excluding custom electricity_price."""
    return {
        CONF_SCAN_INTERVAL_MINUTES: options.get(CONF_SCAN_INTERVAL_MINUTES),
        CONF_ENABLE_RECORDER_STATS: options.get(CONF_ENABLE_RECORDER_STATS),
        CONF_ENABLE_EXTENDED_INSIGHTS: options.get(CONF_ENABLE_EXTENDED_INSIGHTS),
    }


async def async_setup_entry(
    hass: HomeAssistant, entry: SingaporePowerConfigEntry
) -> bool:
    """Set up Singapore Power from a config entry."""
    _LOGGER.info("Setting up Singapore Power integration for %s", entry.title)
    _forget_password(hass, entry)

    session = async_get_clientsession(hass)

    def _persist_tokens(tokens: dict[str, Any]) -> None:
        """Persist rotated OAuth tokens into ConfigEntry.data without storing passwords."""
        base_data = {k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
        new_data = {
            **base_data,
            CONF_ACCESS_TOKEN: tokens.get("access_token"),
            CONF_ID_TOKEN: tokens.get("id_token"),
            CONF_REFRESH_TOKEN: tokens.get("refresh_token"),
            CONF_TOKEN_EXPIRY: tokens.get("token_expiry"),
        }
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=new_data)

    auth = SingaporePowerAuth(
        session,
        entry.data[CONF_USERNAME],
        None,
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        id_token=entry.data.get(CONF_ID_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        token_expiry=entry.data.get(CONF_TOKEN_EXPIRY),
        token_update_callback=_persist_tokens,
    )
    api = SingaporePowerApi(session, auth)

    coordinator = SingaporePowerDataUpdateCoordinator(hass, entry, api)
    try:
        await coordinator.async_config_entry_first_refresh()
    except AuthException as err:
        if err.error_folded == OAUTH_ERROR_REQUIRES_VERIFICATION:
            raise ConfigEntryNotReady(
                str(err), **translated_error("requires_verification", err)
            ) from err
        raise ConfigEntryAuthFailed(
            str(err), **translated_error("auth_failed", err)
        ) from err
    except ApiException as err:
        raise ConfigEntryNotReady(
            str(err), **translated_error("usage_failed", err)
        ) from err

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    prev_non_price = _non_price_options(dict(entry.options))
    prev_price = parse_electricity_price(entry.options.get(CONF_ELECTRICITY_PRICE))

    async def _async_options_updated(
        hass_inner: HomeAssistant, updated_entry: SingaporePowerConfigEntry
    ) -> None:
        nonlocal prev_non_price, prev_price
        curr_opts = dict(updated_entry.options)
        curr_non_price = _non_price_options(curr_opts)
        curr_price = parse_electricity_price(curr_opts.get(CONF_ELECTRICITY_PRICE))

        if curr_non_price == prev_non_price and curr_price != prev_price:
            prev_price = curr_price
            await coordinator.async_handle_price_option_change()
            return

        await hass_inner.config_entries.async_reload(updated_entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SingaporePowerConfigEntry
) -> bool:
    """Unload a Singapore Power config entry and stop background Recorder tasks."""
    coordinator: SingaporePowerDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    ) or hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_stop_stats_import()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
