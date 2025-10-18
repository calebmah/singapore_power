import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .pysingaporepower import SingaporePowerApi, ApiException

_LOGGER = logging.getLogger(__name__)

class SingaporePowerDataUpdateCoordinator(DataUpdateCoordinator):
    """Manages fetching and processing data from the Singapore Power API."""

    def __init__(self, hass: HomeAssistant, api: SingaporePowerApi):
        """Initialize."""
        self.api = api
        self.store = Store(hass, 1, f"{DOMAIN}_coordinator_storage")
        self.premises = []
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
        )

    async def _async_update_data(self):
        """Fetch and process data from the API."""
        _LOGGER.info("Starting data update")
        try:
            if not self.premises:
                _LOGGER.debug("Premise list is empty, fetching from API")
                self.premises = await self.api.async_get_premise_details()

            persisted_data = await self.store.async_load() or {}
            _LOGGER.debug("Loaded persisted data: %s", persisted_data)
            
            all_premise_data = {}

            for premise in self.premises:
                account_no = premise.account_no
                _LOGGER.info("Processing data for account %s", account_no)
                all_premise_data[account_no] = {"active_services": premise.active_services}

                # --- Electricity: Daily Reconciliation ---
                if "electricity" in premise.active_services:
                    _LOGGER.debug("Processing electricity data for account %s", account_no)
                    elec_storage_key = f"total_{account_no}_electricity"
                    last_total_elec = persisted_data.get(elec_storage_key, 0)
                    last_date_str = persisted_data.get(f"{elec_storage_key}_date", (datetime.now() - timedelta(days=1)).isoformat())
                    last_date = dt_util.parse_date(last_date_str)
                    today = dt_util.now().date()

                    if last_date < (today - timedelta(days=1)):
                        _LOGGER.info("Backfilling electricity data for account %s from %s", account_no, last_date)
                        history = await self.api.async_get_consumption_history(
                            premise.premise_no, "electric", dt_util.start_of_day(last_date), dt_util.end_of_day(today - timedelta(days=1))
                        )
                        backfill_sum = sum(c.value for c in history)
                        _LOGGER.debug("Backfill sum: %s kWh", backfill_sum)
                        last_total_elec += backfill_sum

                    todays_history = await self.api.async_get_consumption_history(
                        premise.premise_no, "electric", dt_util.start_of_day(datetime.now()), datetime.now()
                    )
                    todays_total = sum(c.value for c in todays_history)
                    _LOGGER.debug("Today's reconciled electricity consumption: %s kWh", todays_total)
                    new_total_elec = last_total_elec + todays_total
                    
                    all_premise_data[account_no]["electricity_total"] = new_total_elec
                    persisted_data[elec_storage_key] = new_total_elec
                    persisted_data[f"{elec_storage_key}_date"] = today.isoformat()
                    _LOGGER.info("New total electricity for account %s: %s kWh", account_no, new_total_elec)

                # --- Water: Monthly Reconciliation ---
                if "water" in premise.active_services:
                    _LOGGER.debug("Processing water data for account %s", account_no)
                    yearly_summary = await self.api.async_get_yearly_summary(premise.premise_no)
                    water_data = yearly_summary.get("water", {}).get("data", [])
                    
                    total_water = sum(month["consumption"]["current"] for month in water_data)
                    all_premise_data[account_no]["water_total"] = total_water
                    _LOGGER.info("New total water for account %s: %s m³", account_no, total_water)

            await self.store.async_save(persisted_data)
            _LOGGER.debug("Saved persisted data: %s", persisted_data)
            _LOGGER.info("Data update complete")
            return all_premise_data

        except ApiException as err:
            _LOGGER.error("API Error during data update: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception:
            _LOGGER.exception("Unexpected error during data update")
            raise