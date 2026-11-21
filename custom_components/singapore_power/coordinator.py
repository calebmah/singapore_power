"""DataUpdateCoordinator for the Singapore Power integration."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_ELECTRICITY_PRICE,
    CONF_ENABLE_EXTENDED_INSIGHTS,
    CONF_ENABLE_RECORDER_STATS,
    CONF_SCAN_INTERVAL_MINUTES,
    DEFAULT_ENABLE_EXTENDED_INSIGHTS,
    DEFAULT_ENABLE_RECORDER_STATS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    LEDGER_STORAGE_VERSION,
    METADATA_REFRESH_HOURS,
    SLOW_POLL_MS,
    ledger_storage_key,
    parse_electricity_price,
    translated_error,
)
from .pysingaporepower import (
    AccountBillingSummary,
    AmiChartSummary,
    ApiException,
    AuthException,
    Consumption,
    FcuSummary,
    GreenGoalSummary,
    MeterReadingDetail,
    OptionalEcosystemData,
    Premise,
    SingaporePowerApi,
    TariffComparisonSummary,
    UtilityBilledSummary,
)
from .pysingaporepower.const import (
    AMI_DAILY_MONTHS,
    OAUTH_ERROR_REQUIRES_VERIFICATION,
)
from .pysingaporepower.utils import (
    end_of_sg_day,
    merge_ami_daily_and_half_hourly,
    now_sg,
    round_sgd,
    safe_log_text,
    start_of_sg_day,
    start_of_sg_month,
)
from .recorder_stats import (
    async_import_bill_statistics,
    async_import_billed_monthly_statistics,
    async_import_full_series,
    async_import_hourly_statistics,
)

_LOGGER = logging.getLogger(__name__)

_DAILY_RETENTION_DAYS = 75
_MONTHLY_RETENTION_COUNT = 18


def _resolve_point_cost(
    value: float,
    amount_sgd: float | None,
    fallback_rate: float | None,
    *,
    prefer_rate_override: bool = False,
) -> float | None:
    """Resolve SGD cost for a point using custom rate override, SP amount, or tariff fallback."""
    if prefer_rate_override and fallback_rate is not None and fallback_rate > 0:
        return round_sgd(Decimal(str(value)) * Decimal(str(fallback_rate)))
    if amount_sgd is not None:
        return amount_sgd
    if fallback_rate is not None and fallback_rate > 0:
        return round_sgd(Decimal(str(value)) * Decimal(str(fallback_rate)))
    return None


def _resolve_chart_cost(
    chart: AmiChartSummary | None,
    has_ami: bool,
    fallback_rate: float | None,
    *,
    prefer_rate_override: bool = False,
) -> float | None:
    """Resolve total SGD cost for an AMI chart with custom/regulated rate fallback."""
    if not has_ami or chart is None or not chart.points:
        return None
    if prefer_rate_override and fallback_rate is not None and fallback_rate > 0:
        return round_sgd(
            Decimal(str(chart.total_consumption)) * Decimal(str(fallback_rate))
        )
    if chart.total_cost_sgd is not None:
        return chart.total_cost_sgd
    if fallback_rate is not None and fallback_rate > 0:
        return round_sgd(
            Decimal(str(chart.total_consumption)) * Decimal(str(fallback_rate))
        )
    return None


class SingaporePowerDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages fetching, reconciling, and persisting Singapore Power utility & ecosystem data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: SingaporePowerApi,
    ) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.api = api
        self.store: Store[dict[str, Any]] = Store(
            hass, LEDGER_STORAGE_VERSION, ledger_storage_key(entry.entry_id)
        )

        scan_minutes = int(
            entry.options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES)
        )
        self.enable_recorder_stats: bool = bool(
            entry.options.get(CONF_ENABLE_RECORDER_STATS, DEFAULT_ENABLE_RECORDER_STATS)
        )
        self.enable_extended_insights: bool = bool(
            entry.options.get(
                CONF_ENABLE_EXTENDED_INSIGHTS, DEFAULT_ENABLE_EXTENDED_INSIGHTS
            )
        )
        self.custom_electricity_price: float | None = parse_electricity_price(
            entry.options.get(CONF_ELECTRICITY_PRICE)
        )

        self.premises: list[Premise] = []
        self._last_metadata_refresh: datetime | None = None
        self._meter_readings_cache: dict[str, dict[str, MeterReadingDetail]] = {}
        self._green_goals_cache: dict[str, dict[str, GreenGoalSummary]] = {}
        self._ecosystem_cache: OptionalEcosystemData = OptionalEcosystemData()
        self._tariff_cache: dict[str, TariffComparisonSummary] = {}
        self._fcus_cache: dict[str, list[FcuSummary]] = {}

        # Operational diagnostics telemetry
        self.last_error: str | None = None
        self.last_poll_ms: int | None = None
        self.last_success: datetime | None = None

        # Background Recorder import coordination
        self._stats_lock = asyncio.Lock()
        self._stats_task: asyncio.Task[None] | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=scan_minutes),
        )

    def _should_refresh_metadata(self, current_time: datetime) -> bool:
        """Return True if slow-changing metadata should be refreshed on this cycle."""
        if not self.premises or self._last_metadata_refresh is None:
            return True
        return (current_time - self._last_metadata_refresh) >= timedelta(
            hours=METADATA_REFRESH_HOURS
        )

    async def async_handle_price_option_change(self) -> None:
        """Update custom electricity price in place and trigger refresh + Recorder cost rewrite."""
        self.custom_electricity_price = parse_electricity_price(
            self.entry.options.get(CONF_ELECTRICITY_PRICE)
        )
        await self.async_request_refresh()

    async def async_stop_stats_import(self) -> None:
        """Cancel and await any running background Recorder statistics import task."""
        task = self._stats_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._stats_task = None

    def _raise_for_auth_error(self, err: AuthException) -> None:
        """Convert AuthException to UpdateFailed (on verification gate) or ConfigEntryAuthFailed."""
        self.last_error = safe_log_text(err)
        if err.error_folded == OAUTH_ERROR_REQUIRES_VERIFICATION:
            raise UpdateFailed(
                str(err), **translated_error("requires_verification", err)
            ) from err
        raise ConfigEntryAuthFailed(
            str(err), **translated_error("auth_failed", err)
        ) from err

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch and reconcile all Singapore Power utility, billing, and ecosystem metrics."""
        started_mono = time.monotonic()
        now = now_sg()
        today = now.date()
        yesterday = today - timedelta(days=1)

        self.api.clear_read_failures()
        self.custom_electricity_price = parse_electricity_price(
            self.entry.options.get(CONF_ELECTRICITY_PRICE)
        )

        try:
            refresh_meta = self._should_refresh_metadata(now)
            if refresh_meta:
                self.premises = await self.api.async_get_premise_details()

            persisted = await self.store.async_load() or {}
            ledgers: dict[str, Any] = persisted.setdefault("ledgers", {})

            has_active_ev = (
                self._ecosystem_cache.ev_session is not None
                or self._ecosystem_cache.ev_wallet is not None
            )
            refresh_ecosystem = self.enable_extended_insights and (
                refresh_meta or has_active_ev
            )

            tasks: list[Any] = [self.api.async_get_billing_summaries(self.premises)]
            if refresh_meta:
                tasks.append(self.api.async_get_meter_readings(self.premises))
                if self.enable_extended_insights:
                    tasks.append(self.api.async_get_green_goals(self.premises))
            if refresh_ecosystem:
                tasks.append(self.api.async_get_optional_ecosystem())

            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for item in gathered:
                if isinstance(item, AuthException):
                    self._raise_for_auth_error(item)

            idx = 0
            billing_by_acct: dict[str, AccountBillingSummary] = (
                gathered[idx] if isinstance(gathered[idx], dict) else {}
            )
            idx += 1

            if refresh_meta:
                if idx < len(gathered) and isinstance(gathered[idx], dict):
                    self._meter_readings_cache = gathered[idx]
                idx += 1
                if self.enable_extended_insights:
                    if idx < len(gathered) and isinstance(gathered[idx], dict):
                        self._green_goals_cache = gathered[idx]
                    idx += 1
                self._last_metadata_refresh = now

            if refresh_ecosystem and idx < len(gathered):
                if isinstance(gathered[idx], OptionalEcosystemData):
                    self._ecosystem_cache = gathered[idx]

            coordinator_data: dict[str, Any] = {
                "_account": {
                    "greenup": self._ecosystem_cache.greenup,
                    "ecosystem": self._ecosystem_cache,
                    "updated_at": now.isoformat(),
                }
            }

            stats_jobs: list[dict[str, Any]] = []

            for premise in self.premises:
                acct = premise.account_no
                premise_data, premise_stats_jobs = await self._async_process_premise(
                    premise=premise,
                    today=today,
                    yesterday=yesterday,
                    now=now,
                    refresh_meta=refresh_meta,
                    ledgers=ledgers,
                    billing=billing_by_acct.get(acct),
                    meter_readings=self._meter_readings_cache.get(acct, {}),
                    green_goals=self._green_goals_cache.get(acct, {}),
                )
                coordinator_data[acct] = premise_data
                stats_jobs.extend(premise_stats_jobs)

            persisted["updated_at"] = now.isoformat()
            await self.store.async_save(persisted)

            if self.enable_recorder_stats and stats_jobs:
                self._schedule_stats_import(stats_jobs, now)

            elapsed_ms = round((time.monotonic() - started_mono) * 1000)
            self.last_poll_ms = elapsed_ms
            self.last_success = now
            self.last_error = None
            if elapsed_ms > SLOW_POLL_MS:
                _LOGGER.warning("Singapore Power poll took %d ms", elapsed_ms)

            return coordinator_data

        except (ConfigEntryAuthFailed, UpdateFailed):
            self.last_poll_ms = round((time.monotonic() - started_mono) * 1000)
            raise
        except AuthException as err:
            self.last_poll_ms = round((time.monotonic() - started_mono) * 1000)
            self._raise_for_auth_error(err)
            raise AssertionError("unreachable") from err
        except ApiException as err:
            self.last_poll_ms = round((time.monotonic() - started_mono) * 1000)
            self.last_error = safe_log_text(err)
            raise UpdateFailed(
                str(err), **translated_error("usage_failed", err)
            ) from err
        except Exception as err:
            self.last_poll_ms = round((time.monotonic() - started_mono) * 1000)
            self.last_error = safe_log_text(err)
            _LOGGER.exception("Unexpected error updating Singapore Power data")
            raise UpdateFailed(
                str(err), **translated_error("usage_failed", err)
            ) from err

    async def _async_process_premise(
        self,
        *,
        premise: Premise,
        today: date,
        yesterday: date,
        now: datetime,
        refresh_meta: bool,
        ledgers: dict[str, Any],
        billing: AccountBillingSummary | None,
        meter_readings: dict[str, MeterReadingDetail],
        green_goals: dict[str, GreenGoalSummary],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fetch charts for a single premise and compute monotonic totals & sensor values."""
        acct = premise.account_no
        pno = premise.premise_no
        premise_label = f"SP {premise.postal_code or acct}"

        # 1. Fetch 12-month billed summary (/jarvis/v4/charts/{premise_id}) -> covers elec, water, gas
        billed_summaries: dict[str, UtilityBilledSummary] = {}
        if pno:
            try:
                premise_type_from_chart, billed_summaries = (
                    await self.api.async_get_billed_history(pno)
                )
                if premise_type_from_chart and not premise.premise_type:
                    premise.premise_type = premise_type_from_chart
            except AuthException:
                raise
            except Exception as err:
                _LOGGER.warning(
                    "Failed fetching billed history for premise %s: %s",
                    safe_log_text(pno),
                    safe_log_text(err),
                )

        # Ensure any utility with billed data or AMI capability is reflected in active_services
        active_services = list(premise.active_services)
        for util_key, bsum in billed_summaries.items():
            if bsum.months and util_key not in active_services:
                active_services.append(util_key)
        for util_key in (*premise.ami_utilities, *premise.interval_utilities):
            if util_key not in active_services:
                active_services.append(util_key)

        # New-account pre-bill fallback: newly opened accounts may have AMI data before 1st bill
        if not active_services and pno:
            active_services.append("electricity")

        # 2. Refresh SP Regulated Electricity, Gas & PUB Water tariffs BEFORE utility cost computation
        if (
            any(u in active_services for u in ("electricity", "water", "gas"))
            and (refresh_meta or acct not in self._tariff_cache)
        ):
            elec_billed = billed_summaries.get("electricity")
            ref_kwh = (
                elec_billed.average_consumption
                if elec_billed and elec_billed.average_consumption
                else 400.0
            )
            try:
                tariff_res = await self.api.async_get_tariff_comparison(ref_kwh)
                if tariff_res is not None:
                    self._tariff_cache[acct] = tariff_res
            except Exception as err:
                _LOGGER.debug("Tariff comparison unavailable: %s", safe_log_text(err))

        tariff = self._tariff_cache.get(acct)
        effective_elec_rate = (
            self.custom_electricity_price
            if self.custom_electricity_price is not None
            else (tariff.sp_kwh_price if tariff else None)
        )
        effective_gas_rate = tariff.sp_gas_kwh_price if tariff else None
        effective_water_rate = tariff.sp_water_m3_price if tariff else None
        effective_water_tier2_rate = (
            tariff.sp_water_above40_m3_price if tariff else None
        )
        prefer_custom_rate = self.custom_electricity_price is not None

        # 3. Probe paired Tengah Fan Coil Units (FCUs) when insights enabled
        if self.enable_extended_insights and (
            refresh_meta
            or bool(self._fcus_cache.get(acct))
            or "cooling" in active_services
        ):
            fcus = await self.api.async_get_paired_fcus(acct)
            self._fcus_cache[acct] = fcus
            if fcus and "cooling" not in active_services:
                active_services.append("cooling")

        utilities_payload: dict[str, dict[str, Any]] = {}
        all_anomalies: dict[str, list[dict[str, str]]] = {}
        stats_jobs: list[dict[str, Any]] = []

        # 4. Process Electricity, Water, and Gas
        for util_key in ("electricity", "water", "gas"):
            if util_key not in active_services and util_key not in meter_readings:
                continue
            if util_key == "electricity":
                rate = effective_elec_rate
                tier2_rate = None
            elif util_key == "water":
                rate = effective_water_rate
                tier2_rate = effective_water_tier2_rate
            else:
                rate = effective_gas_rate
                tier2_rate = None
            prefer_override = prefer_custom_rate if util_key == "electricity" else False
            util_result, stat_job = await self._async_process_utility(
                premise=premise,
                premise_label=premise_label,
                util_key=util_key,
                today=today,
                yesterday=yesterday,
                now=now,
                refresh_meta=refresh_meta,
                ledgers=ledgers,
                billed_summary=billed_summaries.get(util_key),
                meter_reading=meter_readings.get(util_key),
                fallback_cost_rate=rate,
                tier2_cost_rate=tier2_rate,
                prefer_rate_override=prefer_override,
            )
            utilities_payload[util_key] = util_result
            if util_result.get("anomalies"):
                all_anomalies[util_key] = util_result["anomalies"]
            if stat_job is not None:
                stats_jobs.append(stat_job)

        # 5. Process Tengah Centralized Cooling if active
        if "cooling" in active_services:
            cooling_result = await self._async_process_cooling(
                premise=premise,
                today=today,
                now=now,
                ledgers=ledgers,
            )
            utilities_payload["cooling"] = cooling_result

        # 6. Schedule monthly bill Recorder statistics if bills exist
        if billing and billing.bills:
            stats_jobs.append(
                {
                    "kind": "bills",
                    "account_no": acct,
                    "premise_label": premise_label,
                    "bills": list(billing.bills),
                }
            )

        elec_total = utilities_payload.get("electricity", {}).get("cumulative_total")
        water_total = utilities_payload.get("water", {}).get("cumulative_total")
        gas_total = utilities_payload.get("gas", {}).get("cumulative_total")

        premise_data = {
            "premise": premise,
            "active_services": active_services,
            "electricity_total": elec_total,
            "water_total": water_total,
            "gas_total": gas_total,
            "utilities": utilities_payload,
            "billing": billing,
            "meter_readings": meter_readings,
            "green_goals": green_goals,
            "tariff": tariff,
            "fcus": self._fcus_cache.get(acct, []),
            "anomalies": all_anomalies,
            "updated_at": now.isoformat(),
        }
        return premise_data, stats_jobs

    async def _async_process_utility(
        self,
        *,
        premise: Premise,
        premise_label: str,
        util_key: str,
        today: date,
        yesterday: date,
        now: datetime,
        refresh_meta: bool,
        ledgers: dict[str, Any],
        billed_summary: UtilityBilledSummary | None,
        meter_reading: MeterReadingDetail | None,
        fallback_cost_rate: float | None,
        tier2_cost_rate: float | None,
        prefer_rate_override: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Fetch AMI charts (if supported) and reconcile cumulative ledger for one utility."""
        acct = premise.account_no
        pno = premise.premise_no
        ledger_key = f"{acct}_{util_key}"
        ledger: dict[str, Any] = ledgers.setdefault(ledger_key, {})

        default_unit = "m³" if util_key == "water" else "kWh"
        unit = billed_summary.unit if billed_summary and billed_summary.unit else default_unit

        explicitly_ami = (
            util_key in premise.ami_utilities
            or util_key in premise.interval_utilities
        )
        new_account_elec_probe = (
            util_key == "electricity"
            and not premise.ami_utilities
            and not premise.interval_utilities
            and not (billed_summary and billed_summary.months)
        )
        should_query_ami = bool(pno) and (explicitly_ami or new_account_elec_probe)

        today_chart: AmiChartSummary | None = None
        yesterday_chart: AmiChartSummary | None = None
        mtd_chart: AmiChartSummary | None = None
        prev_month_chart: AmiChartSummary | None = None
        used_ppms = False

        if should_query_ami:
            need_prev_month = not ledger.get("daily_seeded") or today.day <= 5
            prev_month_end = start_of_sg_month(today) - timedelta(seconds=1)
            prev_month_start = start_of_sg_month(prev_month_end)

            coros: list[Any] = [
                self.api.async_get_ami_chart(
                    pno, util_key, start_of_sg_day(today), end_of_sg_day(today), "day", now=now
                ),
                self.api.async_get_ami_chart(
                    pno, util_key, start_of_sg_day(yesterday), end_of_sg_day(yesterday), "day", now=now
                ),
                self.api.async_get_ami_chart(
                    pno, util_key, start_of_sg_month(today), end_of_sg_day(today), "month", now=now
                ),
            ]
            if need_prev_month:
                coros.append(
                    self.api.async_get_ami_chart(
                        pno, util_key, prev_month_start, prev_month_end, "month", now=now
                    )
                )

            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, AuthException):
                    raise r

            today_chart = results[0] if isinstance(results[0], AmiChartSummary) else None
            yesterday_chart = results[1] if isinstance(results[1], AmiChartSummary) else None
            mtd_chart = results[2] if isinstance(results[2], AmiChartSummary) else None
            if need_prev_month and len(results) > 3 and isinstance(results[3], AmiChartSummary):
                prev_month_chart = results[3]

            # PPMS electricity fallback if standard AMI chart returned empty on a PPMS premise
            if (
                premise.ppms
                and util_key == "electricity"
                and (not mtd_chart or not mtd_chart.points)
                and (not today_chart or not today_chart.points)
            ):
                try:
                    today_chart = await self.api.async_get_ami_chart(
                        pno,
                        util_key,
                        start_of_sg_day(today),
                        end_of_sg_day(today),
                        "day",
                        use_ppms=True,
                        now=now,
                    )
                    yesterday_chart = await self.api.async_get_ami_chart(
                        pno,
                        util_key,
                        start_of_sg_day(yesterday),
                        end_of_sg_day(yesterday),
                        "day",
                        use_ppms=True,
                        now=now,
                    )
                    mtd_chart = await self.api.async_get_ami_chart(
                        pno,
                        util_key,
                        start_of_sg_month(today),
                        end_of_sg_day(today),
                        "month",
                        use_ppms=True,
                        now=now,
                    )
                    used_ppms = True
                except Exception:
                    pass

        has_ami_data = explicitly_ami or any(
            c is not None and len(c.points) > 0
            for c in (today_chart, yesterday_chart, mtd_chart, prev_month_chart)
        )

        for chart in (today_chart, mtd_chart, yesterday_chart):
            if chart and chart.unit:
                unit = chart.unit
                break

        cumulative_total = self._reconcile_cumulative_ledger(
            ledger=ledger,
            today=today,
            yesterday=yesterday,
            has_ami_data=has_ami_data,
            today_chart=today_chart,
            yesterday_chart=yesterday_chart,
            mtd_chart=mtd_chart,
            prev_month_chart=prev_month_chart,
            billed_summary=billed_summary,
        )

        # Build Recorder import descriptor (executed asynchronously under _stats_lock)
        interval_pts: list[Consumption] = []
        if yesterday_chart and yesterday_chart.points:
            interval_pts.extend(yesterday_chart.points)
        if today_chart and today_chart.points:
            interval_pts.extend(today_chart.points)

        price_key = (
            f"custom:{self.custom_electricity_price}"
            if (util_key == "electricity" and self.custom_electricity_price is not None)
            else "default"
        )
        stat_job: dict[str, Any] | None = None
        if has_ami_data and pno:
            stat_job = {
                "kind": "ami",
                "ledger_key": ledger_key,
                "account_no": acct,
                "premise_no": pno,
                "premise_label": premise_label,
                "utility_key": util_key,
                "unit": unit,
                "use_ppms": used_ppms,
                "interval_points": interval_pts,
                "fallback_cost_rate": fallback_cost_rate,
                "prefer_rate_override": prefer_rate_override,
                "price_key": price_key,
            }
        elif billed_summary and billed_summary.months:
            stat_job = {
                "kind": "billed_monthly",
                "account_no": acct,
                "premise_label": premise_label,
                "utility_key": util_key,
                "unit": unit,
                "months": list(billed_summary.months),
                "unit_rate": fallback_cost_rate,
                "tier2_unit_rate": tier2_cost_rate,
            }

        latest_pt: Consumption | None = None
        if today_chart and today_chart.latest_point:
            latest_pt = today_chart.latest_point
        elif yesterday_chart and yesterday_chart.latest_point:
            latest_pt = yesterday_chart.latest_point

        anomalies: list[dict[str, str]] = []
        if yesterday_chart:
            anomalies.extend(yesterday_chart.anomalies)
        if today_chart:
            anomalies.extend(today_chart.anomalies)

        latest_billed = billed_summary.latest_month if billed_summary else None

        today_cost = _resolve_chart_cost(
            today_chart,
            has_ami_data,
            fallback_cost_rate,
            prefer_rate_override=prefer_rate_override,
        )
        yesterday_cost = _resolve_chart_cost(
            yesterday_chart,
            has_ami_data,
            fallback_cost_rate,
            prefer_rate_override=prefer_rate_override,
        )
        mtd_cost = _resolve_chart_cost(
            mtd_chart,
            has_ami_data,
            fallback_cost_rate,
            prefer_rate_override=prefer_rate_override,
        )
        latest_pt_cost = (
            _resolve_point_cost(
                latest_pt.value,
                latest_pt.amount_sgd,
                fallback_cost_rate,
                prefer_rate_override=prefer_rate_override,
            )
            if latest_pt is not None
            else None
        )

        payload = {
            "unit": unit,
            "has_ami": has_ami_data,
            "cumulative_total": cumulative_total,
            "today_usage": (
                today_chart.total_consumption
                if (today_chart and has_ami_data and today_chart.points)
                else None
            ),
            "today_cost_sgd": today_cost,
            "today_slots_count": len(today_chart.points) if today_chart else 0,
            "daily_limit": (
                today_chart.daily_limit
                if today_chart and today_chart.daily_limit is not None
                else (mtd_chart.daily_limit if mtd_chart else None)
            ),
            "yesterday_usage": (
                yesterday_chart.total_consumption
                if (yesterday_chart and has_ami_data and yesterday_chart.points)
                else None
            ),
            "yesterday_cost_sgd": yesterday_cost,
            "yesterday_date": yesterday.isoformat(),
            "yesterday_slots": [
                {
                    "time": p.date.strftime("%H:%M"),
                    "value": p.value,
                    "cost_sgd": _resolve_point_cost(
                        p.value,
                        p.amount_sgd,
                        fallback_cost_rate,
                        prefer_rate_override=prefer_rate_override,
                    ),
                }
                for p in (yesterday_chart.points if yesterday_chart else [])
            ],
            "latest_interval_value": latest_pt.value if latest_pt else None,
            "latest_interval_time": latest_pt.date.isoformat() if latest_pt else None,
            "latest_interval_cost_sgd": latest_pt_cost,
            "latest_interval_anomaly": latest_pt.anomaly if latest_pt else None,
            "latest_interval_tags": latest_pt.tags if latest_pt else None,
            "mtd_usage": (
                mtd_chart.total_consumption
                if (mtd_chart and has_ami_data and mtd_chart.points)
                else None
            ),
            "mtd_cost_sgd": mtd_cost,
            "mtd_days_count": len(mtd_chart.points) if mtd_chart else 0,
            "billed_current": latest_billed.current if latest_billed else None,
            "billed_previous": latest_billed.previous if latest_billed else None,
            "billed_period": (
                latest_billed.period_date.strftime("%Y-%m")
                if (latest_billed and latest_billed.period_date)
                else (latest_billed.period if latest_billed else None)
            ),
            "billed_status": latest_billed.status if latest_billed else None,
            "peer_average": latest_billed.peer_average if latest_billed else None,
            "peer_efficient": latest_billed.peer_efficient if latest_billed else None,
            "average_12m": billed_summary.average_consumption if billed_summary else None,
            "comparison_message": (
                billed_summary.comparison_message if billed_summary else None
            ),
            "meter_reading": meter_reading,
            "anomalies": anomalies,
        }
        return payload, stat_job

    def _schedule_stats_import(
        self, jobs: list[dict[str, Any]], now: datetime
    ) -> None:
        """Run initial 13-month backfill or 48-hour live Recorder rewrite in a background task."""
        if self._stats_task is not None and not self._stats_task.done():
            return
        coro = self._async_run_stats_jobs(jobs, now)
        create_bg = getattr(self.hass, "async_create_background_task", None)
        if callable(create_bg):
            self._stats_task = create_bg(
                coro, f"{DOMAIN}_recorder_stats_{self.entry.entry_id}"
            )
        else:
            self._stats_task = self.hass.async_create_task(coro)

    async def _async_run_stats_jobs(
        self, jobs: list[dict[str, Any]], now: datetime
    ) -> None:
        """Execute queued Recorder statistics imports sequentially under _stats_lock."""
        async with self._stats_lock:
            persisted = await self.store.async_load() or {}
            ledgers: dict[str, Any] = persisted.setdefault("ledgers", {})
            dirty = False
            today = now.date()

            for job in jobs:
                kind = job["kind"]
                if kind == "ami":
                    ledger = ledgers.setdefault(job["ledger_key"], {})
                    backfilled = bool(ledger.get("recorder_backfilled"))
                    price_changed = (
                        backfilled
                        and job["utility_key"] == "electricity"
                        and ledger.get("recorder_price_key") != job["price_key"]
                    )

                    if not backfilled or price_changed:
                        try:
                            daily_pts, hh_pts = (
                                await self.api.async_get_full_ami_half_hourly_history(
                                    job["premise_no"],
                                    job["utility_key"],
                                    today,
                                    months_count=AMI_DAILY_MONTHS,
                                    use_ppms=job["use_ppms"],
                                    now=now,
                                )
                            )
                        except AuthException:
                            raise
                        except Exception as err:
                            _LOGGER.warning(
                                "Full 13-month AMI history fetch failed: %s",
                                safe_log_text(err),
                            )
                            daily_pts = []
                            hh_pts = list(job["interval_points"])

                        merged = (
                            merge_ami_daily_and_half_hourly(daily_pts, hh_pts)
                            if (daily_pts and hh_pts)
                            else []
                        )
                        if merged:
                            await async_import_full_series(
                                hass=self.hass,
                                account_no=job["account_no"],
                                premise_label=job["premise_label"],
                                utility_key=job["utility_key"],
                                unit=job["unit"],
                                points=merged,
                                fallback_cost_rate=job["fallback_cost_rate"],
                                prefer_rate_override=job["prefer_rate_override"],
                                cost_only=(price_changed and backfilled),
                            )
                            ledger["recorder_backfilled"] = True
                            ledger["recorder_price_key"] = job["price_key"]
                            dirty = True
                            _LOGGER.info(
                                "Completed initial 13-month Recorder backfill for %s (%s): %d points",
                                job["utility_key"],
                                job["account_no"],
                                len(merged),
                            )
                        elif not backfilled and job["interval_points"]:
                            await async_import_hourly_statistics(
                                hass=self.hass,
                                account_no=job["account_no"],
                                premise_label=job["premise_label"],
                                utility_key=job["utility_key"],
                                unit=job["unit"],
                                interval_points=job["interval_points"],
                                fallback_cost_rate=job["fallback_cost_rate"],
                                prefer_rate_override=job["prefer_rate_override"],
                            )
                    elif job["interval_points"]:
                        await async_import_hourly_statistics(
                            hass=self.hass,
                            account_no=job["account_no"],
                            premise_label=job["premise_label"],
                            utility_key=job["utility_key"],
                            unit=job["unit"],
                            interval_points=job["interval_points"],
                            fallback_cost_rate=job["fallback_cost_rate"],
                            prefer_rate_override=job["prefer_rate_override"],
                        )

                elif kind == "billed_monthly":
                    await async_import_billed_monthly_statistics(
                        hass=self.hass,
                        account_no=job["account_no"],
                        premise_label=job["premise_label"],
                        utility_key=job["utility_key"],
                        unit=job["unit"],
                        months=job["months"],
                        unit_rate=job.get("unit_rate"),
                        tier2_unit_rate=job.get("tier2_unit_rate"),
                    )

                elif kind == "bills":
                    await async_import_bill_statistics(
                        hass=self.hass,
                        account_no=job["account_no"],
                        premise_label=job["premise_label"],
                        bills=job["bills"],
                    )

            if dirty:
                await self.store.async_save(persisted)

    async def _async_process_cooling(
        self,
        *,
        premise: Premise,
        today: date,
        now: datetime,
        ledgers: dict[str, Any],
    ) -> dict[str, Any]:
        """Fetch Tengah centralized cooling consumption and reconcile cumulative total."""
        acct = premise.account_no
        ledger = ledgers.setdefault(f"{acct}_cooling", {})

        today_chart, mtd_chart = await asyncio.gather(
            self.api.async_get_cooling_chart(
                acct, start_of_sg_day(today), end_of_sg_day(today), "day", now=now
            ),
            self.api.async_get_cooling_chart(
                acct, start_of_sg_month(today), end_of_sg_day(today), "month", now=now
            ),
            return_exceptions=True,
        )
        for r in (today_chart, mtd_chart):
            if isinstance(r, AuthException):
                raise r

        t_chart = today_chart if isinstance(today_chart, AmiChartSummary) else None
        m_chart = mtd_chart if isinstance(mtd_chart, AmiChartSummary) else None
        unit = (
            (t_chart.unit if t_chart else None)
            or (m_chart.unit if m_chart else None)
            or "kWh"
        )

        cumulative_total = self._reconcile_cumulative_ledger(
            ledger=ledger,
            today=today,
            yesterday=today - timedelta(days=1),
            has_ami_data=True,
            today_chart=t_chart,
            yesterday_chart=None,
            mtd_chart=m_chart,
            prev_month_chart=None,
            billed_summary=None,
        )

        return {
            "unit": unit,
            "has_ami": True,
            "cumulative_total": cumulative_total,
            "today_usage": (
                t_chart.total_consumption if (t_chart and t_chart.points) else None
            ),
            "mtd_usage": (
                m_chart.total_consumption if (m_chart and m_chart.points) else None
            ),
        }

    @staticmethod
    def _reconcile_cumulative_ledger(
        *,
        ledger: dict[str, Any],
        today: date,
        yesterday: date,
        has_ami_data: bool,
        today_chart: AmiChartSummary | None,
        yesterday_chart: AmiChartSummary | None,
        mtd_chart: AmiChartSummary | None,
        prev_month_chart: AmiChartSummary | None,
        billed_summary: UtilityBilledSummary | None,
    ) -> float | None:
        """Compute a strictly non-decreasing cumulative total keyed by date/month periods."""
        raw_last = ledger.get("last_emitted_total")
        last_emitted = float(raw_last) if raw_last is not None else None

        if has_ami_data:
            daily_totals: dict[str, float] = dict(ledger.get("daily_totals") or {})
            anchor_total = float(ledger.get("daily_anchor_total", 0.0))

            if (
                not ledger.get("daily_seeded")
                and last_emitted is not None
                and last_emitted > 0.0
                and not daily_totals
                and anchor_total == 0.0
            ):
                anchor_total = last_emitted

            for chart in (prev_month_chart, mtd_chart):
                if chart is None:
                    continue
                for pt in chart.points:
                    day_key = pt.date.date().isoformat()
                    daily_totals[day_key] = round(pt.value, 4)

            if yesterday_chart is not None and yesterday_chart.points:
                y_key = yesterday.isoformat()
                daily_totals[y_key] = max(
                    daily_totals.get(y_key, 0.0),
                    round(yesterday_chart.total_consumption, 4),
                )

            if today_chart is not None and today_chart.points:
                t_key = today.isoformat()
                daily_totals[t_key] = max(
                    daily_totals.get(t_key, 0.0),
                    round(today_chart.total_consumption, 4),
                )

            # During an 8pm-8am AMI blackout on initial setup, avoid seeding a 0.0 baseline
            # that would jump by hundreds of kWh when AMI comes online at 8am.
            if not daily_totals and anchor_total == 0.0 and last_emitted is None:
                return None

            cutoff_str = (today - timedelta(days=_DAILY_RETENTION_DAYS)).isoformat()
            for day_key in sorted(list(daily_totals.keys())):
                if day_key < cutoff_str:
                    anchor_total = round(anchor_total + daily_totals.pop(day_key), 4)

            candidate = round(anchor_total + sum(daily_totals.values()), 4)
            emitted = max(last_emitted, candidate) if last_emitted is not None else candidate

            ledger["daily_seeded"] = True
            ledger["daily_anchor_total"] = anchor_total
            ledger["daily_totals"] = daily_totals
            ledger["last_emitted_total"] = emitted
            return emitted

        monthly_totals: dict[str, float] = dict(ledger.get("monthly_totals") or {})
        monthly_anchor = float(ledger.get("monthly_anchor_total", 0.0))

        if billed_summary is not None:
            for m in billed_summary.months:
                if m.status.lower() == "filler":
                    continue
                period_key = (
                    m.period_date.strftime("%Y-%m")
                    if m.period_date is not None
                    else m.period.strip()
                )
                if not period_key:
                    continue
                monthly_totals[period_key] = round(m.current, 4)

        if not monthly_totals and monthly_anchor == 0.0 and last_emitted is None:
            return None

        sorted_periods = sorted(monthly_totals.keys())
        while len(sorted_periods) > _MONTHLY_RETENTION_COUNT:
            oldest = sorted_periods.pop(0)
            monthly_anchor = round(monthly_anchor + monthly_totals.pop(oldest), 4)

        candidate = round(monthly_anchor + sum(monthly_totals.values()), 4)
        emitted = max(last_emitted, candidate) if last_emitted is not None else candidate

        ledger["monthly_anchor_total"] = monthly_anchor
        ledger["monthly_totals"] = monthly_totals
        ledger["last_emitted_total"] = emitted
        return emitted
