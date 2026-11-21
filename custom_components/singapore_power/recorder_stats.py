"""Home Assistant Recorder external statistics helper for Singapore Power smart meter & billing data."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import logging
import re
from typing import Any

from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .pysingaporepower.models import BilledMonthPoint, BillRecord, Consumption
from .pysingaporepower.utils import round_sgd, start_of_sg_day, start_of_sg_month

_LOGGER = logging.getLogger(__name__)


def _sanitize_stat_slug(value: str) -> str:
    """Ensure statistic object_id matches Home Assistant's ^[a-z0-9_]+$ requirement."""
    slug = re.sub(r"[^a-z0-9_]", "_", value.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "account"


def _resolve_ha_unit(unit: str) -> tuple[str, str | None]:
    """Map SP consumption units to Home Assistant Recorder unit and unit_class."""
    if unit in (UnitOfEnergy.KILO_WATT_HOUR, "kWh"):
        return UnitOfEnergy.KILO_WATT_HOUR, "energy"
    if unit in (UnitOfVolume.CUBIC_METERS, "m³"):
        return UnitOfVolume.CUBIC_METERS, "volume"
    return unit, None


def _compute_point_cost_sgd(
    value: float,
    amount_sgd: float | None,
    fallback_rate: float | None,
    *,
    prefer_rate_override: bool = False,
) -> float | None:
    """Compute SGD cost for a single interval/day point with custom/regulated rate fallback."""
    if prefer_rate_override and fallback_rate is not None and fallback_rate > 0:
        return round_sgd(Decimal(str(value)) * Decimal(str(fallback_rate)))
    if amount_sgd is not None:
        return amount_sgd
    if fallback_rate is not None and fallback_rate > 0:
        return round_sgd(Decimal(str(value)) * Decimal(str(fallback_rate)))
    return None


def _build_statistic_metadata(
    statistic_id: str,
    name: str,
    unit: str,
    unit_class: str | None,
) -> Any:
    """Build StatisticMetaData compatibly across Home Assistant 2024.x - 2026.11+."""
    from homeassistant.components.recorder.models import StatisticMetaData

    kwargs: dict[str, Any] = {
        "has_mean": False,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }

    annotations = getattr(StatisticMetaData, "__annotations__", {})
    if "mean_type" in annotations:
        try:
            from homeassistant.components.recorder.models import StatisticMeanType

            kwargs["mean_type"] = StatisticMeanType.NONE
        except ImportError:
            pass
    if "unit_class" in annotations:
        kwargs["unit_class"] = unit_class

    if not isinstance(StatisticMetaData, type) or not issubclass(StatisticMetaData, dict):
        sig = inspect.signature(StatisticMetaData)
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    return StatisticMetaData(**kwargs)


def _fold_points_to_utc_hours(
    points: list[Consumption],
    fallback_rate: float | None,
    *,
    prefer_rate_override: bool = False,
) -> tuple[dict[datetime, float], dict[datetime, float], bool]:
    """Deduplicate points and fold half-hourly/daily slots into exact top-of-hour UTC buckets."""
    deduped = {pt.date: pt for pt in points}
    hourly_consumption: dict[datetime, float] = defaultdict(float)
    hourly_cost: dict[datetime, float] = defaultdict(float)
    has_cost = False

    for pt in deduped.values():
        utc_dt = pt.date.astimezone(UTC)
        hour_bucket = utc_dt.replace(minute=0, second=0, microsecond=0)
        hourly_consumption[hour_bucket] += pt.value
        pt_cost = _compute_point_cost_sgd(
            pt.value,
            pt.amount_sgd,
            fallback_rate,
            prefer_rate_override=prefer_rate_override,
        )
        if pt_cost is not None:
            hourly_cost[hour_bucket] += pt_cost
            has_cost = True

    return hourly_consumption, hourly_cost, has_cost


async def async_import_full_series(
    hass: HomeAssistant,
    account_no: str,
    premise_label: str,
    utility_key: str,
    unit: str,
    points: list[Consumption],
    *,
    fallback_cost_rate: float | None = None,
    prefer_rate_override: bool = False,
    cost_only: bool = False,
) -> None:
    """Import merged 13-month daily + ~100-day half-hourly history starting from 0.0 cumulative sum."""
    if not points or "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticData
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )
    except ImportError:
        _LOGGER.debug("Recorder statistics API unavailable; skipping full series import")
        return

    hourly_cons, hourly_cost, has_cost = _fold_points_to_utc_hours(
        points,
        fallback_cost_rate,
        prefer_rate_override=prefer_rate_override,
    )
    if not hourly_cons:
        return

    acct_slug = _sanitize_stat_slug(account_no)
    util_slug = _sanitize_stat_slug(utility_key)
    cons_stat_id = f"{DOMAIN}:{acct_slug}_{util_slug}_consumption"
    cost_stat_id = f"{DOMAIN}:{acct_slug}_{util_slug}_cost"
    ha_unit, unit_class = _resolve_ha_unit(unit)

    try:
        # Clear target statistic(s) ahead of initial full-series import or custom tariff repricing.
        clear_targets: list[str] = []
        if not cost_only:
            clear_targets.append(cons_stat_id)
        if has_cost and hourly_cost:
            clear_targets.append(cost_stat_id)
        if clear_targets:
            clear_fn = getattr(get_instance(hass), "async_clear_statistics", None)
            if callable(clear_fn):
                clear_fn(clear_targets)
        if not cost_only:
            running_cons = 0.0
            cons_data: list[StatisticData] = []
            for hr in sorted(hourly_cons.keys()):
                val = round(hourly_cons[hr], 4)
                running_cons = round(running_cons + val, 4)
                cons_data.append(StatisticData(start=hr, state=val, sum=running_cons))

            cons_meta = _build_statistic_metadata(
                statistic_id=cons_stat_id,
                name=f"{premise_label} {utility_key.title()} Consumption",
                unit=ha_unit,
                unit_class=unit_class,
            )
            async_add_external_statistics(hass, cons_meta, cons_data)

        if has_cost and hourly_cost:
            running_cost = 0.0
            cost_data: list[StatisticData] = []
            for hr in sorted(hourly_cost.keys()):
                cval = round(hourly_cost[hr], 4)
                running_cost = round(running_cost + cval, 4)
                cost_data.append(StatisticData(start=hr, state=cval, sum=running_cost))

            cost_meta = _build_statistic_metadata(
                statistic_id=cost_stat_id,
                name=f"{premise_label} {utility_key.title()} Cost",
                unit="SGD",
                unit_class=None,
            )
            async_add_external_statistics(hass, cost_meta, cost_data)
    except Exception as err:
        _LOGGER.warning(
            "Failed importing full Recorder series for %s (%s): %s",
            account_no,
            utility_key,
            err,
        )


async def async_import_hourly_statistics(
    hass: HomeAssistant,
    account_no: str,
    premise_label: str,
    utility_key: str,
    unit: str,
    interval_points: list[Consumption],
    *,
    fallback_cost_rate: float | None = None,
    prefer_rate_override: bool = False,
) -> None:
    """Overwrite the live 48-hour sliding window in Recorder, healing delayed early-morning SP uploads."""
    if not interval_points or "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticData
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            statistics_during_period,
        )
    except ImportError:
        _LOGGER.debug("Recorder statistics API unavailable; skipping hourly statistics import")
        return

    hourly_consumption, hourly_cost, has_cost = _fold_points_to_utc_hours(
        interval_points,
        fallback_cost_rate,
        prefer_rate_override=prefer_rate_override,
    )
    if not hourly_consumption:
        return

    sorted_hours = sorted(hourly_consumption.keys())
    earliest_hour = sorted_hours[0]

    acct_slug = _sanitize_stat_slug(account_no)
    util_slug = _sanitize_stat_slug(utility_key)
    cons_stat_id = f"{DOMAIN}:{acct_slug}_{util_slug}_consumption"
    cost_stat_id = f"{DOMAIN}:{acct_slug}_{util_slug}_cost"
    ha_unit, unit_class = _resolve_ha_unit(unit)

    recorder = get_instance(hass)

    async def _baseline_sum_before(stat_id: str, cutoff_utc: datetime) -> float:
        """Fetch cumulative sum from Recorder strictly prior to cutoff_utc."""
        lookback_start = cutoff_utc - timedelta(days=400)
        existing = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            lookback_start,
            cutoff_utc - timedelta(seconds=1),
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = existing.get(stat_id) or []
        if not rows:
            return 0.0
        last_sum = rows[-1].get("sum")
        return float(last_sum) if last_sum is not None else 0.0

    try:
        base_cons_sum = await _baseline_sum_before(cons_stat_id, earliest_hour)
        running_cons = base_cons_sum
        cons_data: list[StatisticData] = []
        for hr in sorted_hours:
            val = round(hourly_consumption[hr], 4)
            running_cons = round(running_cons + val, 4)
            cons_data.append(StatisticData(start=hr, state=val, sum=running_cons))

        cons_meta = _build_statistic_metadata(
            statistic_id=cons_stat_id,
            name=f"{premise_label} {utility_key.title()} Consumption",
            unit=ha_unit,
            unit_class=unit_class,
        )
        async_add_external_statistics(hass, cons_meta, cons_data)

        if has_cost and hourly_cost:
            sorted_cost_hours = sorted(hourly_cost.keys())
            base_cost_sum = await _baseline_sum_before(cost_stat_id, sorted_cost_hours[0])
            running_cost = base_cost_sum
            cost_data: list[StatisticData] = []
            for hr in sorted_cost_hours:
                cval = round(hourly_cost[hr], 4)
                running_cost = round(running_cost + cval, 4)
                cost_data.append(StatisticData(start=hr, state=cval, sum=running_cost))

            cost_meta = _build_statistic_metadata(
                statistic_id=cost_stat_id,
                name=f"{premise_label} {utility_key.title()} Cost",
                unit="SGD",
                unit_class=None,
            )
            async_add_external_statistics(hass, cost_meta, cost_data)
    except Exception as err:
        _LOGGER.warning(
            "Failed importing Recorder hourly statistics for %s (%s): %s",
            account_no,
            utility_key,
            err,
        )


async def async_import_billed_monthly_statistics(
    hass: HomeAssistant,
    account_no: str,
    premise_label: str,
    utility_key: str,
    unit: str,
    months: list[BilledMonthPoint],
) -> None:
    """Import 12-month billed consumption points into Recorder for non-AMI meters (e.g. piped gas)."""
    if not months or "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticData
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            statistics_during_period,
        )
    except ImportError:
        return

    by_bucket: dict[datetime, float] = {}
    for m in months:
        if m.status.lower() == "filler" or m.period_date is None:
            continue
        bucket = start_of_sg_month(m.period_date).astimezone(UTC)
        by_bucket[bucket] = round(m.current, 4)

    if not by_bucket:
        return

    sorted_buckets = sorted(by_bucket.keys())
    earliest = sorted_buckets[0]
    acct_slug = _sanitize_stat_slug(account_no)
    util_slug = _sanitize_stat_slug(utility_key)
    stat_id = f"{DOMAIN}:{acct_slug}_{util_slug}_consumption"
    ha_unit, unit_class = _resolve_ha_unit(unit)

    recorder = get_instance(hass)
    try:
        existing = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            earliest - timedelta(days=800),
            earliest - timedelta(seconds=1),
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = existing.get(stat_id) or []
        running = float(rows[-1]["sum"]) if rows and rows[-1].get("sum") is not None else 0.0

        stat_data: list[StatisticData] = []
        for bkt in sorted_buckets:
            val = by_bucket[bkt]
            running = round(running + val, 4)
            stat_data.append(StatisticData(start=bkt, state=val, sum=running))

        meta = _build_statistic_metadata(
            statistic_id=stat_id,
            name=f"{premise_label} {utility_key.title()} Consumption",
            unit=ha_unit,
            unit_class=unit_class,
        )
        async_add_external_statistics(hass, meta, stat_data)
    except Exception as err:
        _LOGGER.warning(
            "Failed importing monthly billed Recorder statistics for %s (%s): %s",
            account_no,
            utility_key,
            err,
        )


async def async_import_bill_statistics(
    hass: HomeAssistant,
    account_no: str,
    premise_label: str,
    bills: list[BillRecord],
) -> None:
    """Import historical monthly utility bills into Recorder external statistics."""
    if not bills or "recorder" not in hass.config.components:
        return

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.models import StatisticData
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
            statistics_during_period,
        )
    except ImportError:
        return

    by_day_bucket: dict[datetime, float] = {}
    for bill in bills:
        target_date = bill.bill_date or (bill.issued_at.date() if bill.issued_at else None)
        if target_date is None:
            continue
        bucket = start_of_sg_day(target_date).astimezone(UTC)
        by_day_bucket[bucket] = bill.amount_sgd

    if not by_day_bucket:
        return

    sorted_buckets = sorted(by_day_bucket.keys())
    earliest = sorted_buckets[0]
    acct_slug = _sanitize_stat_slug(account_no)
    stat_id = f"{DOMAIN}:{acct_slug}_last_bill"

    recorder = get_instance(hass)
    try:
        existing = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            earliest - timedelta(days=800),
            earliest - timedelta(seconds=1),
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = existing.get(stat_id) or []
        running = float(rows[-1]["sum"]) if rows and rows[-1].get("sum") is not None else 0.0

        stat_data: list[StatisticData] = []
        for bkt in sorted_buckets:
            amt = round(by_day_bucket[bkt], 2)
            running = round(running + amt, 2)
            stat_data.append(StatisticData(start=bkt, state=amt, sum=running))

        meta = _build_statistic_metadata(
            statistic_id=stat_id,
            name=f"{premise_label} Utility Bill",
            unit="SGD",
            unit_class=None,
        )
        async_add_external_statistics(hass, meta, stat_data)
    except Exception as err:
        _LOGGER.warning(
            "Failed importing bill Recorder statistics for %s: %s",
            account_no,
            err,
        )
