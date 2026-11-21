"""Sensor platform for the Singapore Power (SP Group) integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_USERNAME,
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import SingaporePowerDataUpdateCoordinator
from .pysingaporepower import (
    AccountBillingSummary,
    EvChargeSummary,
    EvSessionSummary,
    EvUnpaidSummary,
    EvWalletSummary,
    FcuSummary,
    GreenGoalSummary,
    GreenUpSummary,
    MeterReadingDetail,
    OptionalEcosystemData,
    Premise,
    TariffComparisonSummary,
)
from .pysingaporepower.utils import fcu_sensor_key

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

CURRENCY_SGD = "SGD"
UNIT_SGD_PER_KWH = "SGD/kWh"
UNIT_SGD_PER_M3 = "SGD/m³"
UNIT_LEAVES = "leaves"
UNIT_POINTS = "points"
UNIT_ORDERS = "orders"


def build_premise_device_info(premise: Premise) -> DeviceInfo:
    """Build Home Assistant DeviceInfo grouping all entities for a Singapore Power premise."""
    title_suffix = premise.address or premise.postal_code or premise.account_no
    model_parts = [
        p for p in (premise.premise_type, premise.contestable_type) if p
    ]
    return DeviceInfo(
        identifiers={(DOMAIN, premise.account_no)},
        name=f"SP {title_suffix}",
        manufacturer="SP Group",
        model=" · ".join(model_parts) if model_parts else "Utility Account",
        serial_number=premise.premise_no or premise.account_no,
        configuration_url="https://app.spdigital.sg",
    )


def _build_service_device_info(
    service_slug: str, username: str, label: str, model: str
) -> DeviceInfo:
    """Build Home Assistant DeviceInfo for an account-level SP service (GreenUP, EV, Account)."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{service_slug}_{username.lower()}")},
        name=f"{label} ({username})",
        manufacturer="SP Group",
        model=model,
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://app.spdigital.sg",
    )


def _get_util(premise_data: dict[str, Any], util_key: str) -> dict[str, Any]:
    return (premise_data.get("utilities") or {}).get(util_key) or {}


def _get_billing(premise_data: dict[str, Any]) -> AccountBillingSummary | None:
    val = premise_data.get("billing")
    return val if isinstance(val, AccountBillingSummary) else None


def _get_meter_reading(
    premise_data: dict[str, Any], util_key: str
) -> MeterReadingDetail | None:
    readings = premise_data.get("meter_readings") or {}
    val = readings.get(util_key)
    return val if isinstance(val, MeterReadingDetail) else None


def _get_green_goal(
    premise_data: dict[str, Any], util_key: str
) -> GreenGoalSummary | None:
    goals = premise_data.get("green_goals") or {}
    val = goals.get(util_key)
    return val if isinstance(val, GreenGoalSummary) else None


def _get_tariff(premise_data: dict[str, Any]) -> TariffComparisonSummary | None:
    val = premise_data.get("tariff")
    return val if isinstance(val, TariffComparisonSummary) else None


def _next_meter_read_date(premise_data: dict[str, Any]) -> date | None:
    readings: dict[str, MeterReadingDetail] = premise_data.get("meter_readings") or {}
    candidates: list[date] = []
    for mr in readings.values():
        if mr.next_mnr_date:
            candidates.append(mr.next_mnr_date)
        elif mr.next_mnr_from_date:
            candidates.append(mr.next_mnr_from_date)
        elif mr.smrd_from_date:
            candidates.append(mr.smrd_from_date)
    return min(candidates) if candidates else None


@dataclass(frozen=True, kw_only=True)
class SingaporePowerPremiseSensorDescription(SensorEntityDescription):
    """Describes a Singapore Power premise-scoped sensor."""

    unique_id_suffix: str
    legacy_name_format: str | None = None
    restore_numeric: bool = False
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True
    value_fn: Callable[[dict[str, Any]], Any]
    attributes_fn: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
    dynamic_unit_fn: Callable[[dict[str, Any]], str | None] | None = None
    dynamic_device_class_fn: Callable[[dict[str, Any]], SensorDeviceClass | None] | None = None


def _has_service(util_key: str) -> Callable[[dict[str, Any]], bool]:
    return lambda d: util_key in (d.get("active_services") or []) or bool(_get_util(d, util_key))


def _has_ami(util_key: str) -> Callable[[dict[str, Any]], bool]:
    return lambda d: bool(_get_util(d, util_key).get("has_ami"))


def _has_meter_reading(util_key: str) -> Callable[[dict[str, Any]], bool]:
    return lambda d: (
        (mr := _get_meter_reading(d, util_key)) is not None
        and mr.last_actual_reading is not None
    )


def _gas_unit(d: dict[str, Any]) -> str:
    unit = _get_util(d, "gas").get("unit")
    if unit == "m³":
        return UnitOfVolume.CUBIC_METERS
    return UnitOfEnergy.KILO_WATT_HOUR


def _gas_device_class(d: dict[str, Any]) -> SensorDeviceClass:
    if _gas_unit(d) == UnitOfVolume.CUBIC_METERS:
        return SensorDeviceClass.GAS
    return SensorDeviceClass.ENERGY


def _meter_reading_attrs(util_key: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def _extract(d: dict[str, Any]) -> dict[str, Any] | None:
        mr = _get_meter_reading(d, util_key)
        if mr is None:
            return None
        return {
            "device_no": mr.device_no,
            "last_actual_read_date": (
                mr.last_actual_read_date.isoformat() if mr.last_actual_read_date else None
            ),
            "last_self_submitted": mr.last_self_submitted,
            "last_self_submitted_date": (
                mr.last_self_submitted_date.isoformat()
                if mr.last_self_submitted_date
                else None
            ),
            "bill_month": mr.bill_month,
        }

    return _extract


def _billed_attrs(util_key: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def _extract(d: dict[str, Any]) -> dict[str, Any] | None:
        u = _get_util(d, util_key)
        if not u:
            return None
        return {
            "period": u.get("billed_period"),
            "status": u.get("billed_status"),
            "previous_month_usage": u.get("billed_previous"),
            "neighbour_average": u.get("peer_average"),
            "efficient_neighbour_benchmark": u.get("peer_efficient"),
            "comparison_message": u.get("comparison_message"),
        }

    return _extract


def _latest_interval_attrs(util_key: str) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    def _extract(d: dict[str, Any]) -> dict[str, Any] | None:
        u = _get_util(d, util_key)
        if not u:
            return None
        return {
            "slot_timestamp": u.get("latest_interval_time"),
            "cost_sgd": u.get("latest_interval_cost_sgd"),
            "anomaly": u.get("latest_interval_anomaly"),
            "tags": u.get("latest_interval_tags"),
        }

    return _extract


PREMISE_SENSOR_DESCRIPTIONS: tuple[SingaporePowerPremiseSensorDescription, ...] = (
    # =========================================================================
    # ELECTRICITY SENSORS
    # =========================================================================
    SingaporePowerPremiseSensorDescription(
        key="electricity_total",
        unique_id_suffix="electricity",  # Preserves original entity unique_id!
        legacy_name_format="SP {account_no} Electricity",
        icon="mdi:flash",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        restore_numeric=True,
        exists_fn=_has_service("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("cumulative_total"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_today",
        unique_id_suffix="electricity_today",
        name="Electricity Today",
        icon="mdi:lightning-bolt",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("today_usage"),
        attributes_fn=lambda d: {
            "slots_recorded": _get_util(d, "electricity").get("today_slots_count"),
            "daily_limit_kwh": _get_util(d, "electricity").get("daily_limit"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_cost_today",
        unique_id_suffix="electricity_cost_today",
        name="Electricity Cost Today",
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("today_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_yesterday",
        unique_id_suffix="electricity_yesterday",
        name="Electricity Yesterday",
        icon="mdi:flash-outline",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("yesterday_usage"),
        attributes_fn=lambda d: {
            "date": _get_util(d, "electricity").get("yesterday_date"),
            "cost_sgd": _get_util(d, "electricity").get("yesterday_cost_sgd"),
            "half_hourly_slots": _get_util(d, "electricity").get("yesterday_slots"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_cost_yesterday",
        unique_id_suffix="electricity_cost_yesterday",
        name="Electricity Cost Yesterday",
        icon="mdi:cash",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("yesterday_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_latest_interval",
        unique_id_suffix="electricity_latest_interval",
        name="Electricity Latest 30m Slot",
        icon="mdi:meter-electric",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("latest_interval_value"),
        attributes_fn=_latest_interval_attrs("electricity"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_mtd",
        unique_id_suffix="electricity_mtd",
        name="Electricity Month to Date",
        icon="mdi:calendar-month",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("mtd_usage"),
        attributes_fn=lambda d: {
            "days_recorded": _get_util(d, "electricity").get("mtd_days_count"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_cost_mtd",
        unique_id_suffix="electricity_cost_mtd",
        name="Electricity Cost Month to Date",
        icon="mdi:cash-multiple",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("mtd_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_billed_current",
        unique_id_suffix="electricity_billed_current",
        name="Electricity Last Billed Month",
        icon="mdi:file-document-outline",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        exists_fn=_has_service("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("billed_current"),
        attributes_fn=_billed_attrs("electricity"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_peer_average",
        unique_id_suffix="electricity_peer_average",
        name="Electricity Neighbour Average",
        icon="mdi:home-group",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        exists_fn=_has_service("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("peer_average"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_peer_efficient",
        unique_id_suffix="electricity_peer_efficient",
        name="Electricity Efficient Benchmark",
        icon="mdi:leaf-circle-outline",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        exists_fn=_has_service("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("peer_efficient"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_12m_average",
        unique_id_suffix="electricity_12m_average",
        name="Electricity 12-Month Average",
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        exists_fn=_has_service("electricity"),
        value_fn=lambda d: _get_util(d, "electricity").get("average_12m"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="electricity_meter_reading",
        unique_id_suffix="electricity_meter_reading",
        name="Electricity Meter Reading",
        icon="mdi:counter",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,  # TOTAL avoids false resets after estimated bills
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_meter_reading("electricity"),
        value_fn=lambda d: (
            mr.last_actual_reading if (mr := _get_meter_reading(d, "electricity")) else None
        ),
        attributes_fn=_meter_reading_attrs("electricity"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="sp_electricity_tariff",
        unique_id_suffix="sp_electricity_tariff",
        name="SP Regulated Electricity Tariff",
        icon="mdi:tag-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_SGD_PER_KWH,
        suggested_display_precision=4,
        exists_fn=lambda d: (t := _get_tariff(d)) is not None and t.sp_kwh_price is not None,
        value_fn=lambda d: t.sp_kwh_price if (t := _get_tariff(d)) else None,
        attributes_fn=lambda d: (
            {
                "sp_kwh_price_excl_gst": t.sp_kwh_price_excl_gst,
                "sp_monthly_estimate_sgd": t.sp_monthly_price,
                "reference_consumption_kwh": t.reference_consumption_kwh,
                "sp_gas_kwh_price": t.sp_gas_kwh_price,
                "sp_gas_kwh_price_excl_gst": t.sp_gas_kwh_price_excl_gst,
                "tariff_period": t.tariff_period,
                "tariff_source": t.tariff_source,
                "best_oem_fixed_rate_sgd_kwh": t.best_fixed_rate,
                "best_oem_fixed_retailer": t.best_fixed_retailer,
                "best_oem_fixed_plan": t.best_fixed_plan,
                "best_oem_fixed_monthly_savings_sgd": t.best_fixed_monthly_savings,
                "best_oem_discount_pct": t.best_discount_pct,
                "best_oem_discount_rate_sgd_kwh": t.best_discount_rate,
                "best_oem_discount_retailer": t.best_discount_retailer,
                "best_oem_discount_plan": t.best_discount_plan,
                "available_oem_plans_count": t.total_oem_plans,
            }
            if (t := _get_tariff(d))
            else None
        ),
    ),
    # =========================================================================
    # WATER SENSORS
    # =========================================================================
    SingaporePowerPremiseSensorDescription(
        key="water_total",
        unique_id_suffix="water",  # Preserves original entity unique_id!
        legacy_name_format="SP {account_no} Water",
        icon="mdi:water",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        restore_numeric=True,
        exists_fn=_has_service("water"),
        value_fn=lambda d: _get_util(d, "water").get("cumulative_total"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_today",
        unique_id_suffix="water_today",
        name="Water Today",
        icon="mdi:water-check",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("today_usage"),
        attributes_fn=lambda d: {
            "slots_recorded": _get_util(d, "water").get("today_slots_count"),
            "daily_limit_m3": _get_util(d, "water").get("daily_limit"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_cost_today",
        unique_id_suffix="water_cost_today",
        name="Water Cost Today",
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("today_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_yesterday",
        unique_id_suffix="water_yesterday",
        name="Water Yesterday",
        icon="mdi:water-outline",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("yesterday_usage"),
        attributes_fn=lambda d: {
            "date": _get_util(d, "water").get("yesterday_date"),
            "cost_sgd": _get_util(d, "water").get("yesterday_cost_sgd"),
            "half_hourly_slots": _get_util(d, "water").get("yesterday_slots"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_latest_interval",
        unique_id_suffix="water_latest_interval",
        name="Water Latest 30m Slot",
        icon="mdi:water-pump",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=4,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("latest_interval_value"),
        attributes_fn=_latest_interval_attrs("water"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_mtd",
        unique_id_suffix="water_mtd",
        name="Water Month to Date",
        icon="mdi:calendar-month",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("mtd_usage"),
        attributes_fn=lambda d: {
            "days_recorded": _get_util(d, "water").get("mtd_days_count"),
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_cost_mtd",
        unique_id_suffix="water_cost_mtd",
        name="Water Cost Month to Date",
        icon="mdi:cash-multiple",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("water"),
        value_fn=lambda d: _get_util(d, "water").get("mtd_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_billed_current",
        unique_id_suffix="water_billed_current",
        name="Water Last Billed Month",
        icon="mdi:file-document-outline",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=_has_service("water"),
        value_fn=lambda d: _get_util(d, "water").get("billed_current"),
        attributes_fn=_billed_attrs("water"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_peer_average",
        unique_id_suffix="water_peer_average",
        name="Water Neighbour Average",
        icon="mdi:home-group",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=_has_service("water"),
        value_fn=lambda d: _get_util(d, "water").get("peer_average"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_peer_efficient",
        unique_id_suffix="water_peer_efficient",
        name="Water Efficient Benchmark",
        icon="mdi:leaf-circle-outline",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=_has_service("water"),
        value_fn=lambda d: _get_util(d, "water").get("peer_efficient"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_12m_average",
        unique_id_suffix="water_12m_average",
        name="Water 12-Month Average",
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=_has_service("water"),
        value_fn=lambda d: _get_util(d, "water").get("average_12m"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="water_meter_reading",
        unique_id_suffix="water_meter_reading",
        name="Water Meter Reading",
        icon="mdi:counter",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,  # TOTAL avoids false resets after estimated bills
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=_has_meter_reading("water"),
        value_fn=lambda d: (
            mr.last_actual_reading if (mr := _get_meter_reading(d, "water")) else None
        ),
        attributes_fn=_meter_reading_attrs("water"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="sp_water_tariff",
        unique_id_suffix="sp_water_tariff",
        name="SP Domestic Water Price",
        icon="mdi:tag-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_SGD_PER_M3,
        suggested_display_precision=4,
        exists_fn=lambda d: (
            _has_service("water")(d)
            and (t := _get_tariff(d)) is not None
            and t.sp_water_m3_price is not None
        ),
        value_fn=lambda d: t.sp_water_m3_price if (t := _get_tariff(d)) else None,
        attributes_fn=lambda d: (
            {
                "price_0_to_40_m3_incl_gst": t.sp_water_m3_price,
                "price_0_to_40_m3_excl_gst": t.sp_water_m3_price_excl_gst,
                "price_above_40_m3_incl_gst": t.sp_water_above40_m3_price,
                "price_above_40_m3_excl_gst": t.sp_water_above40_m3_price_excl_gst,
                "water_tariff_0_to_40_excl_gst": t.sp_water_base_tariff_excl_gst,
                "water_conservation_tax_0_to_40_excl_gst": t.sp_water_wct_excl_gst,
                "waterborne_tax_0_to_40_excl_gst": t.sp_water_wbt_excl_gst,
                "water_tariff_above_40_excl_gst": t.sp_water_above40_base_tariff_excl_gst,
                "water_conservation_tax_above_40_excl_gst": t.sp_water_above40_wct_excl_gst,
                "waterborne_tax_above_40_excl_gst": t.sp_water_above40_wbt_excl_gst,
                "tariff_year": t.water_tariff_year,
                "tariff_source": t.water_tariff_source,
            }
            if (t := _get_tariff(d))
            else None
        ),
    ),
    # =========================================================================
    # GAS SENSORS (Dynamic unit/device_class: kWh -> ENERGY, m³ -> GAS)
    # =========================================================================
    SingaporePowerPremiseSensorDescription(
        key="gas_total",
        unique_id_suffix="gas",
        legacy_name_format="SP {account_no} Gas",
        icon="mdi:fire",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        restore_numeric=True,
        exists_fn=_has_service("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("cumulative_total"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_today",
        unique_id_suffix="gas_today",
        name="Gas Today",
        icon="mdi:fire",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        exists_fn=_has_ami("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("today_usage"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_cost_today",
        unique_id_suffix="gas_cost_today",
        name="Gas Cost Today",
        icon="mdi:currency-usd",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("today_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_mtd",
        unique_id_suffix="gas_mtd",
        name="Gas Month to Date",
        icon="mdi:calendar-month",
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        exists_fn=_has_ami("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("mtd_usage"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_cost_mtd",
        unique_id_suffix="gas_cost_mtd",
        name="Gas Cost Month to Date",
        icon="mdi:cash-multiple",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=_has_ami("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("mtd_cost_sgd"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_billed_current",
        unique_id_suffix="gas_billed_current",
        name="Gas Last Billed Month",
        icon="mdi:file-document-outline",
        suggested_display_precision=1,
        exists_fn=_has_service("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("billed_current"),
        attributes_fn=_billed_attrs("gas"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_peer_average",
        unique_id_suffix="gas_peer_average",
        name="Gas Neighbour Average",
        icon="mdi:home-group",
        suggested_display_precision=1,
        exists_fn=_has_service("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("peer_average"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_peer_efficient",
        unique_id_suffix="gas_peer_efficient",
        name="Gas Efficient Benchmark",
        icon="mdi:leaf-circle-outline",
        suggested_display_precision=1,
        exists_fn=_has_service("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("peer_efficient"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_12m_average",
        unique_id_suffix="gas_12m_average",
        name="Gas 12-Month Average",
        icon="mdi:chart-line",
        suggested_display_precision=1,
        exists_fn=_has_service("gas"),
        value_fn=lambda d: _get_util(d, "gas").get("average_12m"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="gas_meter_reading",
        unique_id_suffix="gas_meter_reading",
        name="Gas Meter Reading",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL,  # TOTAL avoids false resets after estimated bills
        suggested_display_precision=2,
        exists_fn=_has_meter_reading("gas"),
        value_fn=lambda d: (
            mr.last_actual_reading if (mr := _get_meter_reading(d, "gas")) else None
        ),
        attributes_fn=_meter_reading_attrs("gas"),
        dynamic_unit_fn=_gas_unit,
        dynamic_device_class_fn=_gas_device_class,
    ),
    SingaporePowerPremiseSensorDescription(
        key="sp_gas_tariff",
        unique_id_suffix="sp_gas_tariff",
        name="SP Piped Gas Tariff",
        icon="mdi:tag-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UNIT_SGD_PER_KWH,
        suggested_display_precision=4,
        exists_fn=lambda d: (
            _has_service("gas")(d)
            and (t := _get_tariff(d)) is not None
            and t.sp_gas_kwh_price is not None
        ),
        value_fn=lambda d: t.sp_gas_kwh_price if (t := _get_tariff(d)) else None,
        attributes_fn=lambda d: (
            {
                "sp_gas_kwh_price_excl_gst": t.sp_gas_kwh_price_excl_gst,
                "tariff_period": t.tariff_period,
                "tariff_source": t.gas_tariff_source or t.tariff_source,
            }
            if (t := _get_tariff(d))
            else None
        ),
    ),
    # =========================================================================
    # TENGAH CENTRALIZED COOLING SENSORS
    # =========================================================================
    SingaporePowerPremiseSensorDescription(
        key="cooling_total",
        unique_id_suffix="cooling",
        name="Centralized Cooling",
        icon="mdi:snowflake",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        restore_numeric=True,
        exists_fn=_has_service("cooling"),
        value_fn=lambda d: _get_util(d, "cooling").get("cumulative_total"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="cooling_today",
        unique_id_suffix="cooling_today",
        name="Cooling Today",
        icon="mdi:air-conditioner",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_service("cooling"),
        value_fn=lambda d: _get_util(d, "cooling").get("today_usage"),
    ),
    SingaporePowerPremiseSensorDescription(
        key="cooling_mtd",
        unique_id_suffix="cooling_mtd",
        name="Cooling Month to Date",
        icon="mdi:snowflake-thermometer",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=_has_service("cooling"),
        value_fn=lambda d: _get_util(d, "cooling").get("mtd_usage"),
    ),
    # =========================================================================
    # BILLING, PAYMENTS, METER SCHEDULE & GREEN GOALS SENSORS
    # =========================================================================
    SingaporePowerPremiseSensorDescription(
        key="outstanding_balance",
        unique_id_suffix="outstanding_balance",
        name="Outstanding Bill Balance",
        icon="mdi:receipt-text-clock-outline",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=lambda d: _get_billing(d) is not None,
        value_fn=lambda d: (
            b.outstanding_amount_sgd if (b := _get_billing(d)) else None
        ),
        attributes_fn=lambda d: (
            {
                "currency": b.currency,
                "giro_enabled": b.giro_enabled,
                "recurring_enabled": b.recurring_enabled,
                "is_owner": b.is_owner,
                "system": b.system,
            }
            if (b := _get_billing(d))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="latest_bill_amount",
        unique_id_suffix="latest_bill_amount",
        name="Latest Bill Amount",
        icon="mdi:receipt-text-outline",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None and b.latest_bill_amount_sgd is not None
        ),
        value_fn=lambda d: b.latest_bill_amount_sgd if (b := _get_billing(d)) else None,
        attributes_fn=lambda d: (
            {
                "bill_date": b.latest_bill_date.isoformat() if b.latest_bill_date else None,
                "due_date": b.latest_bill_due_date.isoformat() if b.latest_bill_due_date else None,
                "period": b.latest_bill_period,
                "pdf_url": b.latest_bill_pdf_url,
            }
            if (b := _get_billing(d))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="latest_bill_due_date",
        unique_id_suffix="latest_bill_due_date",
        name="Latest Bill Due Date",
        icon="mdi:calendar-alert",
        device_class=SensorDeviceClass.DATE,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None and b.latest_bill_due_date is not None
        ),
        value_fn=lambda d: b.latest_bill_due_date if (b := _get_billing(d)) else None,
    ),
    SingaporePowerPremiseSensorDescription(
        key="latest_payment_amount",
        unique_id_suffix="latest_payment_amount",
        name="Latest Payment Amount",
        icon="mdi:credit-card-check-outline",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=lambda d: (
            (b := _get_billing(d)) is not None
            and b.latest_payment_amount_sgd is not None
        ),
        value_fn=lambda d: (
            b.latest_payment_amount_sgd if (b := _get_billing(d)) else None
        ),
        attributes_fn=lambda d: (
            {
                "payment_timestamp": (
                    b.latest_payment_date.isoformat() if b.latest_payment_date else None
                ),
                "payment_method": b.latest_payment_method,
                "status": b.latest_payment_status,
                "channel": b.latest_payment_channel,
                "is_recurring": b.latest_payment_recurring,
                "transaction_id": b.latest_payment_txn_id,
            }
            if (b := _get_billing(d))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="ppms_balance",
        unique_id_suffix="ppms_balance",
        name="Prepaid Credit Balance",
        icon="mdi:wallet-outline",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY_SGD,
        suggested_display_precision=2,
        exists_fn=lambda d: bool(
            (p := d.get("premise")) and p.ppms and _get_billing(d) is not None
        ),
        value_fn=lambda d: b.ppms_balance_sgd if (b := _get_billing(d)) else None,
        attributes_fn=lambda d: (
            {"updated_at": b.ppms_updated_at} if (b := _get_billing(d)) else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="next_meter_read_date",
        unique_id_suffix="next_meter_read_date",
        name="Next Scheduled Meter Reading",
        icon="mdi:calendar-clock",
        device_class=SensorDeviceClass.DATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: _next_meter_read_date(d) is not None,
        value_fn=_next_meter_read_date,
        attributes_fn=lambda d: {
            util: {
                "next_mnr_from": (
                    mr.next_mnr_from_date.isoformat() if mr.next_mnr_from_date else None
                ),
                "next_mnr_to": (
                    mr.next_mnr_to_date.isoformat() if mr.next_mnr_to_date else None
                ),
                "smrd_from": mr.smrd_from_date.isoformat() if mr.smrd_from_date else None,
                "smrd_to": mr.smrd_to_date.isoformat() if mr.smrd_to_date else None,
            }
            for util, mr in (d.get("meter_readings") or {}).items()
            if isinstance(mr, MeterReadingDetail)
        },
    ),
    SingaporePowerPremiseSensorDescription(
        key="greengoal_elec_used",
        unique_id_suffix="greengoal_elec_used",
        name="Green Goal Electricity Used",
        icon="mdi:sprout",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        exists_fn=lambda d: (
            (g := _get_green_goal(d, "electricity")) is not None and g.used is not None
        ),
        value_fn=lambda d: g.used if (g := _get_green_goal(d, "electricity")) else None,
        attributes_fn=lambda d: (
            {
                "target_kwh": g.target,
                "consumption_diff_pct": g.consumption_diff_pct,
                "cost_difference_sgd": g.cost_diff_sgd,
                "month": g.month,
            }
            if (g := _get_green_goal(d, "electricity"))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="greengoal_elec_diff_pct",
        unique_id_suffix="greengoal_elec_diff_pct",
        name="Green Goal Electricity vs Last Year",
        icon="mdi:sprout-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        exists_fn=lambda d: (
            (g := _get_green_goal(d, "electricity")) is not None
            and g.consumption_diff_pct is not None
        ),
        value_fn=lambda d: (
            g.consumption_diff_pct if (g := _get_green_goal(d, "electricity")) else None
        ),
        attributes_fn=lambda d: (
            {
                "used_kwh": g.used,
                "target_kwh": g.target,
                "cost_difference_sgd": g.cost_diff_sgd,
                "month": g.month,
            }
            if (g := _get_green_goal(d, "electricity"))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="greengoal_water_used",
        unique_id_suffix="greengoal_water_used",
        name="Green Goal Water Used",
        icon="mdi:cup-water",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=2,
        exists_fn=lambda d: (
            (g := _get_green_goal(d, "water")) is not None and g.used is not None
        ),
        value_fn=lambda d: g.used if (g := _get_green_goal(d, "water")) else None,
        attributes_fn=lambda d: (
            {
                "target_m3": g.target,
                "consumption_diff_pct": g.consumption_diff_pct,
                "cost_difference_sgd": g.cost_diff_sgd,
                "month": g.month,
            }
            if (g := _get_green_goal(d, "water"))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="greengoal_water_diff_pct",
        unique_id_suffix="greengoal_water_diff_pct",
        name="Green Goal Water vs Last Year",
        icon="mdi:water-percent",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        exists_fn=lambda d: (
            (g := _get_green_goal(d, "water")) is not None
            and g.consumption_diff_pct is not None
        ),
        value_fn=lambda d: (
            g.consumption_diff_pct if (g := _get_green_goal(d, "water")) else None
        ),
        attributes_fn=lambda d: (
            {
                "used_m3": g.used,
                "target_m3": g.target,
                "cost_difference_sgd": g.cost_diff_sgd,
                "month": g.month,
            }
            if (g := _get_green_goal(d, "water"))
            else None
        ),
    ),
    SingaporePowerPremiseSensorDescription(
        key="account_retailer_status",
        unique_id_suffix="retailer",
        name="Electricity Retailer",
        icon="mdi:transmission-tower",
        entity_category=EntityCategory.DIAGNOSTIC,
        exists_fn=lambda d: d.get("premise") is not None,
        value_fn=lambda d: (
            (p.retailer_name or "SP Services")
            if (p := d.get("premise"))
            else None
        ),
        attributes_fn=lambda d: (
            {
                "account_no": p.account_no,
                "premise_no": p.premise_no,
                "address": p.address,
                "postal_code": p.postal_code,
                "premise_type": p.premise_type,
                "contestable_type": p.contestable_type,
                "oem_eligible": p.oem_eligible,
                "account_status": p.account_status,
                "account_type": p.account_type,
                "move_in_date": p.move_in_date,
                "active_services": d.get("active_services"),
                "ami_utilities": p.ami_utilities,
                "interval_utilities": p.interval_utilities,
                "ppms": p.ppms,
            }
            if (p := d.get("premise"))
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Singapore Power sensors and dynamically discover newly activated services."""
    coordinator: SingaporePowerDataUpdateCoordinator = getattr(
        config_entry, "runtime_data", None
    ) or hass.data[DOMAIN][config_entry.entry_id]

    username = str(config_entry.data.get(CONF_USERNAME, config_entry.entry_id))
    known_unique_ids: set[str] = set()

    @callback
    def _async_discover_entities() -> None:
        new_entities: list[SensorEntity] = []
        data = coordinator.data or {}

        for account_no, premise_data in data.items():
            if account_no.startswith("_") or not isinstance(premise_data, dict):
                continue
            premise = premise_data.get("premise")
            if not isinstance(premise, Premise):
                continue

            for desc in PREMISE_SENSOR_DESCRIPTIONS:
                uid = f"{premise.account_no}_{desc.unique_id_suffix}"
                if uid in known_unique_ids or not desc.exists_fn(premise_data):
                    continue
                known_unique_ids.add(uid)
                new_entities.append(
                    SingaporePowerPremiseSensor(coordinator, premise, desc)
                )

            for fcu in premise_data.get("fcus") or []:
                if not isinstance(fcu, FcuSummary):
                    continue
                fcu_uid = f"{premise.account_no}_{fcu_sensor_key(fcu.thing_name)}"
                if fcu_uid in known_unique_ids:
                    continue
                known_unique_ids.add(fcu_uid)
                new_entities.append(
                    SingaporePowerFcuSensor(coordinator, premise, fcu.thing_name)
                )

        acct_meta = data.get("_account") or {}
        ecosystem = acct_meta.get("ecosystem")
        if not isinstance(ecosystem, OptionalEcosystemData):
            ecosystem = OptionalEcosystemData(
                greenup=(
                    acct_meta.get("greenup")
                    if isinstance(acct_meta.get("greenup"), GreenUpSummary)
                    else None
                )
            )

        ukey = username.lower()
        if ecosystem.greenup is not None:
            for cls, suffix in (
                (SingaporePowerGreenUpLeavesSensor, "greenup_leaves"),
                (SingaporePowerGreenUpTierSensor, "greenup_tier"),
            ):
                uid = f"{ukey}_{suffix}"
                if uid not in known_unique_ids:
                    known_unique_ids.add(uid)
                    new_entities.append(cls(coordinator, username))

        if ecosystem.ev_wallet is not None:
            uid = f"{ukey}_ev_wallet"
            if uid not in known_unique_ids:
                known_unique_ids.add(uid)
                new_entities.append(SingaporePowerEvWalletSensor(coordinator, username))

        if ecosystem.ev_session is not None:
            uid = f"{ukey}_ev_session"
            if uid not in known_unique_ids:
                known_unique_ids.add(uid)
                new_entities.append(SingaporePowerEvSessionSensor(coordinator, username))

        if ecosystem.ev_last_charge is not None and ecosystem.ev_last_charge.kwh is not None:
            uid = f"{ukey}_ev_last_charge"
            if uid not in known_unique_ids:
                known_unique_ids.add(uid)
                new_entities.append(
                    SingaporePowerEvLastChargeSensor(coordinator, username)
                )

        if ecosystem.ev_unpaid is not None:
            uid = f"{ukey}_ev_unpaid"
            if uid not in known_unique_ids:
                known_unique_ids.add(uid)
                new_entities.append(SingaporePowerEvUnpaidSensor(coordinator, username))

        if ecosystem.unread_notifications is not None:
            uid = f"{ukey}_unread_notifications"
            if uid not in known_unique_ids:
                known_unique_ids.add(uid)
                new_entities.append(
                    SingaporePowerUnreadNotificationsSensor(coordinator, username)
                )

        if new_entities:
            _LOGGER.info("Adding %d Singapore Power sensor entities", len(new_entities))
            async_add_entities(new_entities)

    _async_discover_entities()
    config_entry.async_on_unload(coordinator.async_add_listener(_async_discover_entities))


class SingaporePowerPremiseSensor(
    CoordinatorEntity[SingaporePowerDataUpdateCoordinator],
    RestoreEntity,
    SensorEntity,
):
    """Representation of a Singapore Power premise utility/billing sensor."""

    entity_description: SingaporePowerPremiseSensorDescription
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        premise: Premise,
        description: SingaporePowerPremiseSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._account_no = premise.account_no
        self._attr_unique_id = f"{premise.account_no}_{description.unique_id_suffix}"
        self._attr_device_info = build_premise_device_info(premise)

        if description.legacy_name_format:
            self._attr_name = description.legacy_name_format.format(
                account_no=premise.account_no
            )
            self._attr_has_entity_name = False
        else:
            self._attr_has_entity_name = True

        self._restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore previous numeric state for monotonic cumulative sensors if needed."""
        await super().async_added_to_hass()
        if not self.entity_description.restore_numeric:
            return
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._restored_value = float(last_state.state)
            except (TypeError, ValueError):
                self._restored_value = None

    def _premise_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._account_no) or {}

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement (supporting dynamic gas units)."""
        if self.entity_description.dynamic_unit_fn is not None:
            return self.entity_description.dynamic_unit_fn(self._premise_data())
        return self.entity_description.native_unit_of_measurement

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return the device class (supporting dynamic gas ENERGY vs GAS validation)."""
        if self.entity_description.dynamic_device_class_fn is not None:
            return self.entity_description.dynamic_device_class_fn(self._premise_data())
        return self.entity_description.device_class

    @property
    def native_value(self) -> Any:
        """Return the current sensor state."""
        pdata = self._premise_data()
        if not pdata:
            return self._restored_value
        val = self.entity_description.value_fn(pdata)
        if val is None and self.entity_description.restore_numeric:
            return self._restored_value
        return val

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional extra state attributes."""
        if self.entity_description.attributes_fn is None:
            return None
        pdata = self._premise_data()
        if not pdata:
            return None
        return self.entity_description.attributes_fn(pdata)


class SingaporePowerFcuSensor(
    CoordinatorEntity[SingaporePowerDataUpdateCoordinator], SensorEntity
):
    """Per-coil Tengah Centralized Cooling Fan Coil Unit (FCU) room temperature / state sensor."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_icon = "mdi:air-conditioner"

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        premise: Premise,
        thing_name: str,
    ) -> None:
        """Initialize the Tengah FCU sensor."""
        super().__init__(coordinator)
        self._account_no = premise.account_no
        self._thing_name = thing_name
        self._attr_unique_id = f"{premise.account_no}_{fcu_sensor_key(thing_name)}"
        self._attr_device_info = build_premise_device_info(premise)
        fcu = self._fcu()
        self._attr_name = (
            fcu.display_name if (fcu and fcu.display_name) else f"FCU {thing_name}"
        )

    def _fcu(self) -> FcuSummary | None:
        pdata = (self.coordinator.data or {}).get(self._account_no) or {}
        for item in pdata.get("fcus") or []:
            if isinstance(item, FcuSummary) and item.thing_name == self._thing_name:
                return item
        return None

    @property
    def device_class(self) -> SensorDeviceClass | None:
        fcu = self._fcu()
        if fcu is not None and fcu.room_temperature is not None:
            return SensorDeviceClass.TEMPERATURE
        return None

    @property
    def state_class(self) -> SensorStateClass | None:
        fcu = self._fcu()
        if fcu is not None and fcu.room_temperature is not None:
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        fcu = self._fcu()
        if fcu is not None and fcu.room_temperature is not None:
            return UnitOfTemperature.CELSIUS
        return None

    @property
    def suggested_display_precision(self) -> int | None:
        fcu = self._fcu()
        if fcu is not None and fcu.room_temperature is not None:
            return 1
        return None

    @property
    def native_value(self) -> float | str | None:
        fcu = self._fcu()
        if fcu is None:
            return None
        if fcu.room_temperature is not None:
            return fcu.room_temperature
        if fcu.is_on is True:
            return "on"
        if fcu.is_on is False:
            return "off"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fcu = self._fcu()
        if fcu is None:
            return None
        return {
            "thing_name": fcu.thing_name,
            "display_name": fcu.display_name,
            "is_on": fcu.is_on,
            "is_online": fcu.is_online,
            "setpoint_c": fcu.setpoint,
            "operation_mode": fcu.mode,
        }


class _AccountEcosystemBaseSensor(
    CoordinatorEntity[SingaporePowerDataUpdateCoordinator], SensorEntity
):
    """Base sensor for account-level SP ecosystem services."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def _ecosystem(self) -> OptionalEcosystemData | None:
        acct = (self.coordinator.data or {}).get("_account") or {}
        eco = acct.get("ecosystem")
        return eco if isinstance(eco, OptionalEcosystemData) else None


class _GreenUpBaseSensor(_AccountEcosystemBaseSensor):
    """Base sensor for SP GreenUP account-level loyalty metrics."""

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        username: str,
    ) -> None:
        super().__init__(coordinator)
        self._username = username
        self._attr_device_info = _build_service_device_info(
            "greenup", username, "SP GreenUP", "GreenUP Loyalty Program"
        )

    def _greenup(self) -> GreenUpSummary | None:
        eco = self._ecosystem()
        if eco is not None and eco.greenup is not None:
            return eco.greenup
        acct = (self.coordinator.data or {}).get("_account") or {}
        val = acct.get("greenup")
        return val if isinstance(val, GreenUpSummary) else None


class SingaporePowerGreenUpLeavesSensor(_GreenUpBaseSensor):
    """Total SP GreenUP leaves balance."""

    _attr_name = "GreenUP Leaves"
    _attr_icon = "mdi:leaf"
    _attr_native_unit_of_measurement = UNIT_LEAVES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_greenup_leaves"

    @property
    def native_value(self) -> int | None:
        g = self._greenup()
        return g.total_points if g else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        g = self._greenup()
        if not g:
            return None
        return {
            "tier_name": g.tier_name,
            "tier_level": g.tier_level,
            "points_to_level_up": g.points_to_level_up,
            "safe_points": g.safe_points,
            "projected_level_status": g.projected_level_status,
        }


class SingaporePowerGreenUpTierSensor(_GreenUpBaseSensor):
    """Current SP GreenUP loyalty tier."""

    _attr_name = "GreenUP Tier"
    _attr_icon = "mdi:medal-outline"

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_greenup_tier"

    @property
    def native_value(self) -> str | None:
        g = self._greenup()
        return g.tier_name if g else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        g = self._greenup()
        if not g:
            return None
        return {
            "tier_level": g.tier_level,
            "description": g.tier_description,
            "points_to_level_up": g.points_to_level_up,
        }


class _EvBaseSensor(_AccountEcosystemBaseSensor):
    """Base sensor for SP EV Charging & EV Wallet metrics."""

    def __init__(
        self,
        coordinator: SingaporePowerDataUpdateCoordinator,
        username: str,
    ) -> None:
        super().__init__(coordinator)
        self._username = username
        self._attr_device_info = _build_service_device_info(
            "ev", username, "SP EV Charging", "EV Charging & Rewards"
        )


class SingaporePowerEvWalletSensor(_EvBaseSensor):
    """SP EV rewards wallet points balance."""

    _attr_name = "EV Wallet Points"
    _attr_icon = "mdi:ev-station"
    _attr_native_unit_of_measurement = UNIT_POINTS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_ev_wallet"

    def _wallet(self) -> EvWalletSummary | None:
        eco = self._ecosystem()
        return eco.ev_wallet if eco else None

    @property
    def native_value(self) -> float | None:
        w = self._wallet()
        return w.points if w else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        w = self._wallet()
        if not w:
            return None
        return {
            "dollar_balance_sgd": w.dollar_balance,
            "current_tier_id": w.current_tier_id,
        }


class SingaporePowerEvSessionSensor(_EvBaseSensor):
    """Latest SP EV charging session status."""

    _attr_name = "EV Latest Session"
    _attr_icon = "mdi:car-electric"

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_ev_session"

    def _session(self) -> EvSessionSummary | None:
        eco = self._ecosystem()
        return eco.ev_session if eco else None

    @property
    def native_value(self) -> str | None:
        s = self._session()
        if s is None:
            return None
        return s.status or ("active" if s.kwh is not None else None)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        s = self._session()
        if s is None:
            return None
        return {
            "kwh": s.kwh,
            "total_cost": s.total_cost,
            "start": s.start,
            "order_id": s.order_id,
        }


class SingaporePowerEvLastChargeSensor(_EvBaseSensor):
    """Energy delivered in the most recent completed SP EV charging receipt."""

    _attr_name = "EV Last Charge"
    _attr_icon = "mdi:battery-charging"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_ev_last_charge"

    def _charge(self) -> EvChargeSummary | None:
        eco = self._ecosystem()
        return eco.ev_last_charge if eco else None

    @property
    def native_value(self) -> float | None:
        c = self._charge()
        return c.kwh if c else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        c = self._charge()
        if c is None:
            return None
        return {
            "amount_sgd": c.amount_sgd,
            "created_at": c.created_at,
            "status": c.status,
            "address": c.address,
        }


class SingaporePowerEvUnpaidSensor(_EvBaseSensor):
    """Unpaid SP EV charging orders amount or count."""

    _attr_name = "EV Unpaid Orders"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator, username)
        self._attr_unique_id = f"{username.lower()}_ev_unpaid"

    def _unpaid(self) -> EvUnpaidSummary | None:
        eco = self._ecosystem()
        return eco.ev_unpaid if eco else None

    @property
    def device_class(self) -> SensorDeviceClass | None:
        u = self._unpaid()
        if u is not None and u.amount_sgd is not None:
            return SensorDeviceClass.MONETARY
        return None

    @property
    def state_class(self) -> SensorStateClass | None:
        u = self._unpaid()
        if u is None:
            return None
        return (
            SensorStateClass.TOTAL
            if u.amount_sgd is not None
            else SensorStateClass.MEASUREMENT
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        u = self._unpaid()
        if u is None:
            return None
        return CURRENCY_SGD if u.amount_sgd is not None else UNIT_ORDERS

    @property
    def native_value(self) -> float | int | None:
        u = self._unpaid()
        if u is None:
            return None
        return u.amount_sgd if u.amount_sgd is not None else u.count

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        u = self._unpaid()
        if u is None:
            return None
        return {
            "unpaid_orders_count": u.count,
            "amount_sgd": u.amount_sgd,
        }


class SingaporePowerUnreadNotificationsSensor(_AccountEcosystemBaseSensor):
    """Unread in-app SP notifications count (diagnostic; never stores message bodies)."""

    _attr_name = "Unread Notifications"
    _attr_icon = "mdi:bell-badge-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SingaporePowerDataUpdateCoordinator, username: str
    ) -> None:
        super().__init__(coordinator)
        self._username = username
        self._attr_unique_id = f"{username.lower()}_unread_notifications"
        self._attr_device_info = _build_service_device_info(
            "account", username, "SP Account", "SP Digital Account"
        )

    @property
    def native_value(self) -> int | None:
        eco = self._ecosystem()
        return eco.unread_notifications if eco else None
