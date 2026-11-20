import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import UnitOfVolume

from .const import DOMAIN
from .coordinator import SingaporePowerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Singapore Power sensors from a config entry."""
    _LOGGER.info("Setting up Singapore Power sensor platform")
    coordinator: SingaporePowerDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    
    entities_to_add = []
    
    _LOGGER.debug("Processing %d premises from coordinator", len(coordinator.data))
    for account_no, premise_data in coordinator.data.items():
        active_services = premise_data.get("active_services", [])
        _LOGGER.debug("Premise %s has active services: %s", account_no, active_services)
        
        if "electricity" in active_services:
            entities_to_add.append(
                SingaporePowerElectricitySensor(coordinator, account_no)
            )
        if "water" in active_services:
            entities_to_add.append(
                SingaporePowerWaterSensor(coordinator, account_no)
            )
    
    _LOGGER.info("Adding %d sensors to Home Assistant", len(entities_to_add))
    async_add_entities(entities_to_add)


class SingaporePowerElectricitySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of a Singapore Power electricity sensor."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_unit_of_measurement = "kWh"
    _attr_icon = "mdi:flash"

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        account_no: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._account_no = account_no
        self._attr_unique_id = f"{self._account_no}_electricity"
        self._attr_name = f"SP {self._account_no} Electricity"
        _LOGGER.debug("Initializing electricity sensor: %s", self.name)

    async def async_added_to_hass(self) -> None:
        """Handle entity which provides NON-additive history."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            _LOGGER.debug("Restored state for %s: %s", self.name, self._attr_native_value)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        try:
            value = self.coordinator.data[self._account_no]["electricity_total"]
            _LOGGER.debug("Value for %s: %s", self.name, value)
            return value
        except KeyError:
            _LOGGER.warning("Could not find electricity_total for %s in coordinator data. Returning last known value.", self.name)
            return self._attr_native_value


class SingaporePowerWaterSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of a Singapore Power water sensor."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.WATER
    _attr_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:water"

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        account_no: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._account_no = account_no
        self._attr_unique_id = f"{self._account_no}_water"
        self._attr_name = f"SP {self._account_no} Water"
        _LOGGER.debug("Initializing water sensor: %s", self.name)

    async def async_added_to_hass(self) -> None:
        """Handle entity which provides NON-additive history."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_native_value = last_state.state
            _LOGGER.debug("Restored state for %s: %s", self.name, self._attr_native_value)

    @property
    def native_value(self):
        """Return the state of the sensor."""
        try:
            value = self.coordinator.data[self._account_no]["water_total"]
            _LOGGER.debug("Value for %s: %s", self.name, value)
            return value
        except KeyError:
            _LOGGER.warning("Could not find water_total for %s in coordinator data. Returning last known value.", self.name)
            return self._attr_native_value
