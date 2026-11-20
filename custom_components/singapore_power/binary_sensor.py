"""Binary sensor platform for the Singapore Power (SP Group) integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import SingaporePowerDataUpdateCoordinator
from .pysingaporepower import AccountBillingSummary, MeterReadingDetail, Premise
from .sensor import build_premise_device_info

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


def _get_billing(premise_data: dict[str, Any]) -> AccountBillingSummary | None:
    val = premise_data.get("billing")
    return val if isinstance(val, AccountBillingSummary) else None


def _any_smrd_window(premise_data: dict[str, Any]) -> bool | None:
    readings: dict[str, MeterReadingDetail] = premise_data.get("meter_readings") or {}
    if not readings:
        return None
    return any(mr.is_smrd_window for mr in readings.values())


@dataclass(frozen=True, kw_only=True)
class SingaporePowerBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Singapore Power premise binary sensor."""

    unique_id_suffix: str
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True
    is_on_fn: Callable[[dict[str, Any]], bool | None]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None


BINARY_SENSOR_DESCRIPTIONS: tuple[SingaporePowerBinarySensorDescription, ...] = (
    SingaporePowerBinarySensorDescription(
        key="giro_enabled",
        unique_id_suffix="giro_enabled",
        name="GIRO Enabled",
        icon="mdi:bank-check",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None and b.giro_enabled is not None
        ),
        is_on_fn=lambda d: b.giro_enabled if (b := _get_billing(d)) else None,
    ),
    SingaporePowerBinarySensorDescription(
        key="recurring_enabled",
        unique_id_suffix="recurring_enabled",
        name="Recurring Card Payment",
        icon="mdi:credit-card-sync-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None and b.recurring_enabled is not None
        ),
        is_on_fn=lambda d: b.recurring_enabled if (b := _get_billing(d)) else None,
    ),
    SingaporePowerBinarySensorDescription(
        key="ebill_enabled",
        unique_id_suffix="ebill_enabled",
        name="Paperless e-Bill",
        icon="mdi:email-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None and b.ebill_enabled is not None
        ),
        is_on_fn=lambda d: b.ebill_enabled if (b := _get_billing(d)) else None,
        attributes_fn=lambda d: (
            {"paper_bill_enabled": b.paper_bill_enabled}
            if (b := _get_billing(d))
            else None
        ),
    ),
    SingaporePowerBinarySensorDescription(
        key="ami_smart_meter",
        unique_id_suffix="ami_smart_meter",
        name="Smart Meter (AMI)",
        icon="mdi:meter-electric-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: d.get("premise") is not None,
        is_on_fn=lambda d: bool(
            ((p := d.get("premise")) and (p.ami_utilities or p.interval_utilities))
            or any(
                u.get("has_ami")
                for u in (d.get("utilities") or {}).values()
                if isinstance(u, dict)
            )
        ),
        attributes_fn=lambda d: (
            {
                "ami_utilities": p.ami_utilities,
                "interval_utilities": p.interval_utilities,
            }
            if (p := d.get("premise"))
            else None
        ),
    ),
    SingaporePowerBinarySensorDescription(
        key="smrd_window_open",
        unique_id_suffix="smrd_window_open",
        name="Self Meter Reading Window",
        icon="mdi:camera-metering-spot",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: bool(d.get("meter_readings")),
        is_on_fn=_any_smrd_window,
        attributes_fn=lambda d: {
            util: {
                "is_open": mr.is_smrd_window,
                "from": mr.smrd_from_date.isoformat() if mr.smrd_from_date else None,
                "to": mr.smrd_to_date.isoformat() if mr.smrd_to_date else None,
            }
            for util, mr in (d.get("meter_readings") or {}).items()
            if isinstance(mr, MeterReadingDetail)
        },
    ),
    SingaporePowerBinarySensorDescription(
        key="usage_anomaly",
        unique_id_suffix="usage_anomaly",
        name="Smart Meter Usage Anomaly",
        icon="mdi:pipe-leak",
        device_class=BinarySensorDeviceClass.PROBLEM,
        exists_fn=lambda d: any(
            u.get("has_ami")
            for u in (d.get("utilities") or {}).values()
            if isinstance(u, dict)
        ),
        is_on_fn=lambda d: bool(d.get("anomalies")),
        attributes_fn=lambda d: {"anomalies": d.get("anomalies") or {}},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Singapore Power binary sensors and dynamically discover newly eligible entities."""
    coordinator: SingaporePowerDataUpdateCoordinator = getattr(
        config_entry, "runtime_data", None
    ) or hass.data[DOMAIN][config_entry.entry_id]

    known_unique_ids: set[str] = set()

    @callback
    def _async_discover_binary_sensors() -> None:
        new_entities: list[BinarySensorEntity] = []
        data = coordinator.data or {}

        for account_no, premise_data in data.items():
            if account_no.startswith("_") or not isinstance(premise_data, dict):
                continue
            premise = premise_data.get("premise")
            if not isinstance(premise, Premise):
                continue

            for desc in BINARY_SENSOR_DESCRIPTIONS:
                uid = f"{premise.account_no}_{desc.unique_id_suffix}"
                if uid in known_unique_ids or not desc.exists_fn(premise_data):
                    continue
                known_unique_ids.add(uid)
                new_entities.append(
                    SingaporePowerPremiseBinarySensor(coordinator, premise, desc)
                )

        if new_entities:
            _LOGGER.info(
                "Adding %d Singapore Power binary sensor entities", len(new_entities)
            )
            async_add_entities(new_entities)

    _async_discover_binary_sensors()
    config_entry.async_on_unload(
        coordinator.async_add_listener(_async_discover_binary_sensors)
    )


class SingaporePowerPremiseBinarySensor(
    CoordinatorEntity[SingaporePowerDataUpdateCoordinator], BinarySensorEntity
):
    """Representation of a Singapore Power premise binary sensor."""

    entity_description: SingaporePowerBinarySensorDescription
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        premise: Premise,
        description: SingaporePowerBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._account_no = premise.account_no
        self._attr_unique_id = f"{premise.account_no}_{description.unique_id_suffix}"
        self._attr_device_info = build_premise_device_info(premise)

    def _premise_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._account_no) or {}

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        pdata = self._premise_data()
        if not pdata:
            return None
        return self.entity_description.is_on_fn(pdata)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional extra state attributes."""
        if self.entity_description.attributes_fn is None:
            return None
        pdata = self._premise_data()
        if not pdata:
            return None
        return self.entity_description.attributes_fn(pdata)
