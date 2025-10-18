import logging
import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .coordinator import SingaporePowerDataUpdateCoordinator
from .pysingaporepower import SingaporePowerAuth, SingaporePowerApi

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Singapore Power from a config entry."""
    _LOGGER.info("Setting up Singapore Power integration for entry %s", entry.entry_id)
    
    session = async_get_clientsession(hass)
    auth = SingaporePowerAuth(
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    api = SingaporePowerApi(session, auth)

    coordinator = SingaporePowerDataUpdateCoordinator(hass, api)
    _LOGGER.debug("Initiating first data refresh")
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("First data refresh complete")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    _LOGGER.debug("Forwarding setup to sensor platform")
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    _LOGGER.info("Singapore Power integration setup complete")
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Singapore Power integration for entry %s", entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        _LOGGER.debug("Successfully removed coordinator and unloaded sensor platform")

    return unload_ok
