"""Main API client for Singapore Power."""

import aiohttp
import logging
from datetime import datetime

from .auth import SingaporePowerAuth
from .const import GRAPHQL_URL, CONSUMPTION_URL, YEARLY_SUMMARY_URL
from .exceptions import ApiException
from .models import Premise, Consumption
from .utils import format_api_date

_LOGGER = logging.getLogger(__name__)

class SingaporePowerApi:
    """Singapore Power API Client."""

    def __init__(self, session: aiohttp.ClientSession, auth: SingaporePowerAuth):
        """Initialize the API client."""
        self._session = session
        self._auth = auth

    async def _request(self, method: str, url: str, **kwargs):
        """Make an authenticated request to the API."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {await self._auth.get_access_token()}"
        id_token = self._auth.get_id_token()
        if id_token:
            headers["x-id-token"] = id_token
        
        _LOGGER.debug("Requesting %s %s with payload %s", method, url, kwargs.get('json'))
        async with self._session.request(method, url, **kwargs, headers=headers) as resp:
            if resp.status >= 400:
                _LOGGER.error("API request failed: %s %s", resp.status, await resp.text())
                raise ApiException(f"API request failed: {resp.status}")
            
            response_data = await resp.json()
            _LOGGER.debug("API Response: %s", response_data)
            return response_data

    async def async_get_premise_details(self) -> list[Premise]:
        """Fetch premise and account details from the GraphQL endpoint."""
        _LOGGER.info("Fetching premise details")
        query = """
        query {
          premiseDetails {
            premises {
                accountNo
                active
                accounts {
                    premiseNo
                }
                activeServices
            }
          }
        }
        """
        payload = {"query": query}
        response_data = await self._request("POST", GRAPHQL_URL, json=payload)
        
        processed_premises = []
        premises_raw = response_data.get("data", {}).get("premiseDetails", {}).get("premises", [])
        
        for premise_raw in premises_raw:
            if premise_raw.get("active"):
                processed_premises.append(Premise(
                    account_no=premise_raw.get("accountNo"),
                    premise_no=premise_raw.get("accounts", [{}])[0].get("premiseNo"),
                    active_services=premise_raw.get("activeServices", [])
                ))
        
        _LOGGER.info("Found %d active premises", len(processed_premises))
        return processed_premises

    async def async_get_consumption_history(
        self, premise_no: str, utility_type: str, start_date: datetime, end_date: datetime
    ) -> list[Consumption]:
        """Fetch consumption history for a given period."""
        _LOGGER.info("Fetching %s consumption for premise %s from %s to %s", utility_type, premise_no, start_date, end_date)
        payload = {
            "start": format_api_date(start_date),
            "end": format_api_date(end_date),
            "grouped_by": "day",
            "premise_id": premise_no,
            "utility_type": utility_type,
        }
        
        response_data = await self._request("POST", CONSUMPTION_URL, json=payload)
        
        consumption_list = []
        for history_item in response_data.get("history", []):
            for data_point in history_item.get("data", []):
                consumption_list.append(Consumption(
                    date=datetime.fromisoformat(data_point["date"]),
                    value=data_point["consumption"]
                ))
        _LOGGER.debug("Found %d consumption data points", len(consumption_list))
        return consumption_list

    async def async_get_yearly_summary(self, premise_no: str) -> dict:
        """Fetch the yearly summary for a given premise."""
        _LOGGER.info("Fetching yearly summary for premise %s", premise_no)
        url = YEARLY_SUMMARY_URL.format(premise_id=premise_no)
        return await self._request("GET", url)
