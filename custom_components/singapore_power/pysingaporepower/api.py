"""Main API client for Singapore Power (SP App v15.13.1 + hardened BFF & EV/Tengah ecosystem)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
import logging
import re
from typing import Any

import aiohttp

from .auth import SingaporePowerAuth
from .const import (
    AMI_DAILY_MONTHS,
    AMI_HALF_HOUR_MONTHS,
    B2C_PRICE_PLAN_URL,
    BILL_PREFERENCES_URL,
    CONSUMPTION_URL,
    DATAGOV_TARIFF_URL,
    EVA_CHARGE_HISTORY_URL,
    EVA_LATEST_SESSION_URL,
    EVA_UNPAID_URL,
    FROSTY_CONSUMPTION_URL,
    FROSTY_FCU_STATUS_URL,
    FROSTY_GRAPHQL_URL,
    GRAPHQL_URL,
    GREEN_GOALS_URL,
    HH_BACKFILL_BATCH_SIZE,
    HTTP_TIMEOUT_SECONDS,
    JARVIS_METER_READINGS_V2_URL,
    JARVIS_PPMS_BALANCE_URL,
    JSON_ENCODING,
    MAX_FCU_STATUS_READS,
    MAX_RESPONSE_BYTES,
    METER_READ_DETAILS_URL,
    NJORD_PPMS_BALANCE_URL,
    NOTIFICATIONS_URL,
    ONEUP_AUTH_GRAPHQL_URL,
    OPTIONAL_HTTP_TIMEOUT_SECONDS,
    PAYABLES_URL,
    PPMS_CHARTS_URL,
    PRICE_PLAN_URL,
    QUERY_GREENUP_ACCOUNT_V2,
    QUERY_YCS_GET_METER_TYPE,
    QUERY_YCS_GET_PREMISE_TYPE,
    QUERY_YCS_PREMISES_DETAILS_V3,
    SCOPE_NOT_FOUND,
    SG_GST_MULTIPLIER,
    SINGSTAT_GAS_TARIFF_URL,
    SINGSTAT_TARIFF_URL,
    SINGSTAT_WATER_PRICE_URL,
    SPGROUP_TARIFF_PAGE_URL,
    TENGAH_PAIRED_FCUS_QUERY,
    TRANSPORT_ERROR_CHARS,
    TYCHE_WALLET_URL,
    USER_AGENT,
    UTILITIES_HISTORY_URL,
    UTILITY_ACCOUNT_URL,
    YEARLY_SUMMARY_URL,
)
from .exceptions import ApiException, AuthException, RateLimitException
from .models import (
    AccountBillingSummary,
    AmiChartSummary,
    BilledMonthPoint,
    BillRecord,
    Consumption,
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
    UtilityBilledSummary,
)
from .utils import (
    account_digits,
    cents_to_dollars,
    drop_future_points,
    end_of_sg_day,
    fold_text,
    format_api_date,
    month_windows_sg,
    months_ago_sg_start,
    normalize_account_no,
    normalize_unit,
    now_sg,
    parse_eva_sgd,
    parse_sp_date,
    parse_sp_datetime,
    quote_path_segment,
    redact_url_for_log,
    round_half_up,
    round_sgd,
    safe_float,
    safe_log_text,
    start_of_sg_day,
    trim_unreported_points,
)

_LOGGER = logging.getLogger(__name__)


def _canonical_service_name(raw: str | None) -> str | None:
    """Normalize SP utility/service identifiers to canonical lowercase names."""
    if not raw:
        return None
    val = fold_text(str(raw))
    if val in (
        "elec",
        "elect",
        "electric",
        "electricity",
        "ami_electric",
        "ami_electricity",
        "smmu_electric",
        "e",
    ):
        return "electricity"
    if val in ("water", "ami_water", "smmu_water", "w"):
        return "water"
    if val in ("gas", "ami_gas", "smmu_gas", "g"):
        return "gas"
    if val in ("cooling", "district_cooling", "centralized_cooling"):
        return "cooling"
    if val in ("refuse", "trash"):
        return "refuse"
    return val


def _extract_graphql_errors(body: Any) -> str:
    """Extract concatenated GraphQL error messages from a 200 OK GraphQL envelope."""
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if not isinstance(errors, list) or not errors:
        return ""
    msgs = [
        safe_log_text(row.get("message"))
        for row in errors
        if isinstance(row, dict) and row.get("message")
    ]
    return "; ".join(m for m in msgs if m)


def _is_scope_denied(status: int, body: Any) -> bool:
    """Return True if an HTTP 403 indicates missing OAuth grant scope rather than broken API."""
    if status != 403 or not isinstance(body, dict):
        return False
    err = fold_text(str(body.get("error") or ""))
    desc = fold_text(str(body.get("error_description") or ""))
    return err == SCOPE_NOT_FOUND or SCOPE_NOT_FOUND in desc


class SingaporePowerApi:
    """Singapore Power asynchronous API client with bounded reads and failure diagnostics."""

    def __init__(self, session: aiohttp.ClientSession, auth: SingaporePowerAuth) -> None:
        """Initialize the API client."""
        self._session = session
        self._auth = auth
        self._read_failures: dict[str, str] = {}

    @property
    def read_failures(self) -> dict[str, str]:
        """Return redacted route -> failure reason for non-404 optional read failures in the current poll."""
        return dict(sorted(self._read_failures.items()))

    def clear_read_failures(self) -> None:
        """Reset optional read failure tracker at the start of a poll cycle."""
        self._read_failures.clear()

    def _note_read_failure(self, method: str, url: str, reason: str) -> None:
        label = f"{method} {redact_url_for_log(url)}"
        self._read_failures[label] = safe_log_text(reason, TRANSPORT_ERROR_CHARS)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        retry_on_401: bool = True,
        timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> Any:
        """Execute a size-bounded, BOM-tolerant HTTP request against SP's MuleSoft / Public APIs."""
        headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("User-Agent", USER_AGENT)
        headers.setdefault("Accept", "application/json")

        if authenticated:
            access_token = await self._auth.get_access_token()
            headers["Authorization"] = f"Bearer {access_token}"
            id_token = self._auth.get_id_token()
            if id_token:
                headers["X-id-token"] = id_token

        redacted = redact_url_for_log(url)

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **kwargs,
            ) as resp:
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise ApiException(
                            f"{method} {redacted} exceeded {MAX_RESPONSE_BYTES} bytes"
                        )
                raw_bytes = bytes(buf)

                if (
                    resp.status == 401
                    and authenticated
                    and retry_on_401
                    and self._auth.refresh_token
                ):
                    _LOGGER.debug(
                        "HTTP 401 on %s %s; refreshing token and retrying once",
                        method,
                        redacted,
                    )
                    await self._auth.async_force_refresh()
                    return await self._request(
                        method,
                        url,
                        authenticated=authenticated,
                        retry_on_401=False,
                        timeout_seconds=timeout_seconds,
                        headers=headers,
                        **kwargs,
                    )

                text = raw_bytes.decode(JSON_ENCODING, errors="replace") if raw_bytes else ""
                parsed_body: Any = {}
                if text:
                    try:
                        parsed_body = json.loads(text)
                    except json.JSONDecodeError as exc:
                        if resp.status < 400:
                            raise ApiException(
                                f"{method} {redacted} returned invalid JSON ({safe_log_text(exc)})",
                                status_code=resp.status,
                            ) from exc
                        parsed_body = {}

                if resp.status == 401 and authenticated:
                    detail = safe_log_text(
                        parsed_body.get("error_description")
                        or parsed_body.get("error")
                        or parsed_body.get("message")
                        or "unauthorized"
                        if isinstance(parsed_body, dict)
                        else "unauthorized"
                    )
                    raise AuthException(
                        f"Unauthorized calling {redacted}: {detail}",
                        status_code=401,
                    )

                if resp.status == 429:
                    raise RateLimitException(
                        f"Rate limited calling {redacted}", status_code=429
                    )

                if resp.status >= 400:
                    if isinstance(parsed_body, dict) and _is_scope_denied(
                        resp.status, parsed_body
                    ):
                        raise ApiException(
                            f"Scope not granted for {redacted}",
                            status_code=403,
                        )
                    msg = (
                        safe_log_text(
                            parsed_body.get("error_description")
                            or parsed_body.get("error")
                            or parsed_body.get("message")
                            or ""
                        )
                        if isinstance(parsed_body, dict)
                        else ""
                    )
                    suffix = f": {msg}" if msg else ""
                    raise ApiException(
                        f"{method} {redacted} HTTP {resp.status}{suffix}",
                        status_code=resp.status,
                    )

                if resp.status == 204 or not text:
                    return {}

                return parsed_body
        except (AuthException, ApiException):
            raise
        except TimeoutError as err:
            raise ApiException(
                f"{method} {redacted} timed out after {timeout_seconds}s"
            ) from err
        except aiohttp.ClientError as err:
            raise ApiException(
                f"{method} {redacted} network error: {safe_log_text(err, TRANSPORT_ERROR_CHARS)}"
            ) from err

    async def _optional_request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        timeout_seconds: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
        record_failure: bool = True,
        **kwargs: Any,
    ) -> Any | None:
        """Execute a best-effort optional read, recording unexpected failures for HA diagnostics."""
        try:
            body = await self._request(
                method,
                url,
                authenticated=authenticated,
                timeout_seconds=timeout_seconds,
                **kwargs,
            )
            gql_err = _extract_graphql_errors(body)
            if gql_err:
                redacted = redact_url_for_log(url)
                folded_err = fold_text(gql_err)
                # Non-Tengah accounts receive GraphQL "Unauthorized" on Frosty FCU queries
                if folded_err in ("unauthorized", "forbidden", "not authorized"):
                    _LOGGER.debug("%s %s not enrolled (%s)", method, redacted, gql_err)
                    return None
                if record_failure:
                    _LOGGER.warning("%s %s returned GraphQL errors: %s", method, redacted, gql_err)
                    self._note_read_failure(method, url, f"GraphQL error: {gql_err}")
                else:
                    _LOGGER.debug("%s %s GraphQL error: %s", method, redacted, gql_err)
            return body
        except AuthException:
            raise
        except ApiException as err:
            # 404 (not enrolled) and 403 scope_not_found are normal steady-state absences
            if err.status_code == 404 or "Scope not granted" in str(err) or not record_failure:
                _LOGGER.debug("Optional read absent (%s)", err)
                return None
            _LOGGER.warning("Optional read failed: %s", err)
            self._note_read_failure(method, url, str(err))
            return None
        except Exception as err:
            _LOGGER.warning(
                "Unexpected error on optional read %s %s: %s",
                method,
                redact_url_for_log(url),
                safe_log_text(err, TRANSPORT_ERROR_CHARS),
            )
            self._note_read_failure(method, url, str(err))
            return None

    # -------------------------------------------------------------------------
    # Premise, Account & Meter Capability Discovery
    # -------------------------------------------------------------------------

    async def async_get_premise_details(self) -> list[Premise]:
        """Fetch active premises and enrich with Jarvis /v3/me, meter types, and premise types."""
        ycs_task = self._request(
            "POST", GRAPHQL_URL, json={"query": QUERY_YCS_PREMISES_DETAILS_V3}
        )
        me_task = self._request("GET", UTILITY_ACCOUNT_URL)
        ycs_res, me_res = await asyncio.gather(ycs_task, me_task, return_exceptions=True)

        premises_by_acct: dict[str, Premise] = {}

        if isinstance(ycs_res, dict):
            gql_err = _extract_graphql_errors(ycs_res)
            if gql_err:
                _LOGGER.warning("Yggdrasil premiseDetails GraphQL error: %s", gql_err)
            raw_premises = (
                (ycs_res.get("data") or {})
                .get("premiseDetails", {})
                .get("premises")
                or []
            )
            for p_raw in raw_premises:
                if not isinstance(p_raw, dict) or not p_raw.get("active", True):
                    continue
                accounts = [a for a in (p_raw.get("accounts") or []) if isinstance(a, dict)]
                first_acct = accounts[0] if accounts else {}
                account_no = str(
                    p_raw.get("accountNo") or first_acct.get("accountNo") or ""
                ).strip()
                premise_no = str(first_acct.get("premiseNo") or "").strip()
                if not account_no and not premise_no:
                    continue

                raw_services: list[str] = []
                for acc in accounts:
                    for svc in acc.get("activeServices") or []:
                        canon = _canonical_service_name(svc)
                        if canon and canon not in raw_services:
                            raw_services.append(canon)
                for svc in p_raw.get("activeServices") or []:
                    canon = _canonical_service_name(svc)
                    if canon and canon not in raw_services:
                        raw_services.append(canon)

                addr_obj = first_acct.get("premiseAddress") or {}
                postal = addr_obj.get("postalCode") if isinstance(addr_obj, dict) else None
                full_addr = first_acct.get("premiseAddressFull")
                if not full_addr and isinstance(addr_obj, dict) and addr_obj:
                    parts = []
                    if addr_obj.get("blockNo") or addr_obj.get("street"):
                        parts.append(
                            f"{addr_obj.get('blockNo', '')} {addr_obj.get('street', '')}".strip()
                        )
                    if addr_obj.get("floor") and addr_obj.get("unitNo"):
                        parts.append(f"#{addr_obj['floor']}-{addr_obj['unitNo']}")
                    if postal:
                        parts.append(f"S{postal}")
                    full_addr = ", ".join(p for p in parts if p)

                shared = p_raw.get("sharedResource") or {}
                is_owner = bool(shared.get("owner", True)) if isinstance(shared, dict) else True

                premises_by_acct[normalize_account_no(account_no or premise_no)] = Premise(
                    account_no=account_no or premise_no,
                    premise_no=premise_no,
                    active_services=raw_services,
                    address=full_addr,
                    postal_code=postal,
                    contestable_type=p_raw.get("contestableType"),
                    retailer_name=first_acct.get("retailerName"),
                    retailer_id=first_acct.get("retailerIdentifier"),
                    account_status=first_acct.get("accountStatus"),
                    account_type=first_acct.get("accountType"),
                    ppms=any(bool(a.get("ppms")) for a in accounts),
                    move_in_date=first_acct.get("moveInDate"),
                    move_out_date=first_acct.get("moveOutDate"),
                    ebs_account_no=first_acct.get("ebsAccountNo"),
                    mssl_account_no=first_acct.get("msslAccountNo"),
                    bp_no=first_acct.get("bpNo"),
                    system=first_acct.get("system"),
                    is_owner=is_owner,
                )
        elif isinstance(ycs_res, AuthException):
            raise ycs_res

        if isinstance(me_res, dict):
            for p_me in me_res.get("premises") or []:
                if not isinstance(p_me, dict) or not p_me.get("active", True):
                    continue
                me_accounts = [a for a in (p_me.get("accounts") or []) if isinstance(a, dict)]
                first_me_acc = me_accounts[0] if me_accounts else {}
                acct_no = str(
                    p_me.get("account_no")
                    or first_me_acc.get("account_number")
                    or ""
                ).strip()
                prem_no = str(
                    p_me.get("id") or first_me_acc.get("premise_number") or ""
                ).strip()
                norm_key = normalize_account_no(acct_no or prem_no)
                if not norm_key:
                    continue

                me_services: list[str] = []
                for svc in p_me.get("utility_types") or []:
                    canon = _canonical_service_name(svc)
                    if canon and canon not in me_services:
                        me_services.append(canon)
                for acc in me_accounts:
                    for svc in acc.get("utilities") or []:
                        canon = _canonical_service_name(svc)
                        if canon and canon not in me_services:
                            me_services.append(canon)

                meter_details = p_me.get("meter_details") if isinstance(p_me.get("meter_details"), dict) else {}
                ami_utils: list[str] = []
                interval_utils: list[str] = []
                if meter_details.get("ami_elec"):
                    ami_utils.append("electricity")
                for u in meter_details.get("interval_utilities") or []:
                    canon = _canonical_service_name(u)
                    if canon and canon not in interval_utils:
                        interval_utils.append(canon)
                    if isinstance(u, str) and "ami" in u.lower() and canon and canon not in ami_utils:
                        ami_utils.append(canon)

                contestable = p_me.get("contestable_details") if isinstance(p_me.get("contestable_details"), dict) else {}
                ppms_details = p_me.get("ppms_details") if isinstance(p_me.get("ppms_details"), dict) else {}
                addr_details = p_me.get("address_details") if isinstance(p_me.get("address_details"), dict) else {}

                existing = premises_by_acct.get(norm_key)
                if existing is None:
                    premises_by_acct[norm_key] = Premise(
                        account_no=acct_no or prem_no,
                        premise_no=prem_no,
                        active_services=me_services,
                        address=p_me.get("address") or first_me_acc.get("address"),
                        postal_code=addr_details.get("postal"),
                        premise_type=p_me.get("type"),
                        contestable_type=contestable.get("contestable_type"),
                        retailer_name=contestable.get("retailer_name"),
                        retailer_id=contestable.get("retailer_identifier"),
                        account_status=first_me_acc.get("account_status"),
                        account_type=first_me_acc.get("account_type"),
                        oem_eligible=p_me.get("oem_eligible") if isinstance(p_me.get("oem_eligible"), bool) else None,
                        ppms=bool(ppms_details.get("exists", False)),
                        move_in_date=first_me_acc.get("move_in_date"),
                        ami_utilities=ami_utils,
                        interval_utilities=interval_utils,
                        ebs_account_no=first_me_acc.get("ebs_account_no"),
                        mssl_account_no=first_me_acc.get("mssl_account_no"),
                        bp_no=first_me_acc.get("bp_number"),
                        system=first_me_acc.get("system"),
                    )
                else:
                    if not existing.premise_no and prem_no:
                        existing.premise_no = prem_no
                    for svc in me_services:
                        if svc not in existing.active_services:
                            existing.active_services.append(svc)
                    if not existing.address and p_me.get("address"):
                        existing.address = p_me.get("address")
                    if not existing.postal_code and addr_details.get("postal"):
                        existing.postal_code = addr_details.get("postal")
                    if not existing.premise_type and p_me.get("type"):
                        existing.premise_type = p_me.get("type")
                    if existing.oem_eligible is None and isinstance(p_me.get("oem_eligible"), bool):
                        existing.oem_eligible = p_me["oem_eligible"]
                    if ppms_details.get("exists") is True:
                        existing.ppms = True
                    if not existing.retailer_name and contestable.get("retailer_name"):
                        existing.retailer_name = contestable.get("retailer_name")
                    if not existing.contestable_type and contestable.get("contestable_type"):
                        existing.contestable_type = contestable.get("contestable_type")
                    for u in ami_utils:
                        if u not in existing.ami_utilities:
                            existing.ami_utilities.append(u)
                    for u in interval_utils:
                        if u not in existing.interval_utilities:
                            existing.interval_utilities.append(u)
        elif isinstance(me_res, AuthException) and not premises_by_acct:
            raise me_res

        premises = list(premises_by_acct.values())
        if not premises and isinstance(ycs_res, Exception) and isinstance(me_res, Exception):
            raise ApiException(f"Failed to fetch premises: {ycs_res}")

        await asyncio.gather(
            *(self._async_enrich_premise_metadata(p) for p in premises),
            return_exceptions=True,
        )

        _LOGGER.info("Discovered %d active Singapore Power premise(s)", len(premises))
        return premises

    async def _async_enrich_premise_metadata(self, premise: Premise) -> None:
        """Enrich a Premise with Yggdrasil meterTypeV1 and premiseTypeForOwnerV1."""
        if not premise.account_no:
            return
        variables = {"accountNo": premise.account_no}
        meter_res, ptype_res = await asyncio.gather(
            self._optional_request(
                "POST",
                GRAPHQL_URL,
                json={"query": QUERY_YCS_GET_METER_TYPE, "variables": variables},
            ),
            self._optional_request(
                "POST",
                GRAPHQL_URL,
                json={"query": QUERY_YCS_GET_PREMISE_TYPE, "variables": variables},
            ),
            return_exceptions=True,
        )

        if isinstance(meter_res, dict):
            mt = ((meter_res.get("data") or {}).get("meterTypeV1")) or {}
            for u in mt.get("ami") or []:
                canon = _canonical_service_name(u)
                if canon and canon not in premise.ami_utilities:
                    premise.ami_utilities.append(canon)
            for u in mt.get("interval") or []:
                canon = _canonical_service_name(u)
                if canon and canon not in premise.interval_utilities:
                    premise.interval_utilities.append(canon)

        if isinstance(ptype_res, dict) and not premise.premise_type:
            pt = (ptype_res.get("data") or {}).get("premiseTypeForOwnerV1")
            if pt and isinstance(pt, str):
                premise.premise_type = pt

    # -------------------------------------------------------------------------
    # Smart Meter (AMI), Cooling & Monthly Billed Consumption
    # -------------------------------------------------------------------------

    async def async_get_ami_chart(
        self,
        premise_no: str,
        utility_type: str,
        start_date: datetime,
        end_date: datetime,
        grouped_by: str = "day",
        *,
        use_ppms: bool = False,
        now: datetime | None = None,
    ) -> AmiChartSummary:
        """Fetch AMI consumption & SGD cost chart, dropping future & trailing 0.0 padded slots."""
        api_utility = (
            "electric"
            if utility_type in ("electricity", "electric", "elec")
            else utility_type
        )
        payload = {
            "premise_id": premise_no,
            "start": format_api_date(start_date),
            "end": format_api_date(end_date),
            "grouped_by": grouped_by,
            "utility_type": api_utility,
        }
        url = PPMS_CHARTS_URL if use_ppms else CONSUMPTION_URL
        response_data = await self._optional_request(
            "POST", url, json=payload, timeout_seconds=HTTP_TIMEOUT_SECONDS
        )
        default_unit = "m³" if api_utility == "water" else "kWh"
        if not isinstance(response_data, dict):
            return AmiChartSummary(
                utility_type=api_utility, grouped_by=grouped_by, unit=default_unit
            )

        unit = normalize_unit(response_data.get("unit"), default_unit)
        # Guard against SP's BFF returning Electricity (kWh) AMI data when queried for non-AMI water
        if api_utility == "water" and unit == "kWh":
            _LOGGER.debug(
                "Ignoring kWh AMI chart response returned for non-AMI water request"
            )
            return AmiChartSummary(
                utility_type="water", grouped_by=grouped_by, unit="m³"
            )
        daily_limit = safe_float(response_data.get("daily_limit"))

        points: list[Consumption] = []
        for history_item in response_data.get("history") or []:
            if not isinstance(history_item, dict):
                continue
            for dp in history_item.get("data") or []:
                if not isinstance(dp, dict):
                    continue
                val = safe_float(dp.get("consumption"))
                if val is None:
                    continue
                dt = parse_sp_datetime(dp.get("date"))
                if dt is None:
                    continue
                raw_amt = safe_float(dp.get("amount"))
                amount_sgd = round_sgd(raw_amt) if raw_amt is not None else None
                anomaly_str: str | None = None
                tags_str: str | None = None
                for meta in dp.get("metadata") or []:
                    if isinstance(meta, dict):
                        if meta.get("anomaly"):
                            anomaly_str = str(meta["anomaly"])
                        if meta.get("tags"):
                            tags_str = str(meta["tags"])
                points.append(
                    Consumption(
                        date=dt,
                        value=val,
                        amount_sgd=amount_sgd,
                        anomaly=anomaly_str,
                        tags=tags_str,
                        raw_date=dp.get("date"),
                    )
                )

        cutoff = now or now_sg()
        points = drop_future_points(points, cutoff)
        recent_cutoff = start_of_sg_day(cutoff - timedelta(days=2))
        if grouped_by in ("day", "month") and end_date >= recent_cutoff:
            points = trim_unreported_points(points)
        else:
            points.sort(key=lambda p: p.date)

        return AmiChartSummary(
            utility_type=api_utility,
            grouped_by=grouped_by,
            unit=unit,
            daily_limit=daily_limit,
            points=points,
        )

    async def async_get_full_ami_half_hourly_history(
        self,
        premise_no: str,
        utility_type: str,
        today: date,
        *,
        months_count: int = AMI_DAILY_MONTHS,
        hh_months_count: int = AMI_HALF_HOUR_MONTHS,
        use_ppms: bool = False,
        now: datetime | None = None,
    ) -> tuple[list[Consumption], list[Consumption]]:
        """Fetch 13-month daily + maximum-retention (~5 months) half-hourly AMI points."""
        cutoff = now or now_sg()
        daily_start = months_ago_sg_start(today, months_count)
        day_end = end_of_sg_day(today)

        daily_chart = await self.async_get_ami_chart(
            premise_no,
            utility_type,
            daily_start,
            day_end,
            "month",
            use_ppms=use_ppms,
            now=cutoff,
        )

        windows = month_windows_sg(today, min(months_count, hh_months_count), end_cap=day_end)
        all_hh_pts: list[Consumption] = []

        for idx in range(0, len(windows), HH_BACKFILL_BATCH_SIZE):
            batch = windows[idx : idx + HH_BACKFILL_BATCH_SIZE]
            batch_res = await asyncio.gather(
                *(
                    self.async_get_ami_chart(
                        premise_no,
                        utility_type,
                        w_start,
                        w_end,
                        "day",
                        use_ppms=use_ppms,
                        now=cutoff,
                    )
                    for w_start, w_end in batch
                ),
                return_exceptions=True,
            )
            for item in batch_res:
                if isinstance(item, AuthException):
                    raise item
                if isinstance(item, AmiChartSummary) and item.points:
                    all_hh_pts.extend(item.points)

        return daily_chart.points, all_hh_pts

    async def async_get_consumption_history(
        self,
        premise_no: str,
        utility_type: str,
        start_date: datetime,
        end_date: datetime,
        grouped_by: str = "day",
    ) -> list[Consumption]:
        """Backwards-compatible helper returning a list of Consumption data points."""
        chart = await self.async_get_ami_chart(
            premise_no, utility_type, start_date, end_date, grouped_by=grouped_by
        )
        return chart.points

    async def async_get_cooling_chart(
        self,
        account_no: str,
        start_date: datetime,
        end_date: datetime,
        grouped_by: str = "day",
        *,
        now: datetime | None = None,
    ) -> AmiChartSummary:
        """Fetch Tengah centralized cooling consumption from /frosty/consumption."""
        params = {
            "utility_acc_id": account_no,
            "grouped_by": grouped_by,
            "start": format_api_date(start_date),
            "end": format_api_date(end_date),
        }
        response_data = await self._optional_request(
            "GET",
            FROSTY_CONSUMPTION_URL,
            params=params,
            timeout_seconds=HTTP_TIMEOUT_SECONDS,
        )
        if not isinstance(response_data, dict):
            return AmiChartSummary(utility_type="cooling", grouped_by=grouped_by, unit="kWh")

        unit = normalize_unit(response_data.get("unit"), "kWh")
        points: list[Consumption] = []
        for history_item in response_data.get("history") or []:
            if not isinstance(history_item, dict):
                continue
            for dp in history_item.get("data") or []:
                if not isinstance(dp, dict):
                    continue
                val = safe_float(dp.get("consumption"))
                if val is None:
                    continue
                dt = parse_sp_datetime(dp.get("time") or dp.get("date"))
                if dt is None:
                    continue
                points.append(
                    Consumption(
                        date=dt,
                        value=val,
                        raw_date=dp.get("time") or dp.get("date"),
                    )
                )

        cutoff = now or now_sg()
        points = drop_future_points(points, cutoff)
        if grouped_by in ("day", "month"):
            points = trim_unreported_points(points)
        else:
            points.sort(key=lambda p: p.date)

        return AmiChartSummary(
            utility_type="cooling",
            grouped_by=grouped_by,
            unit=unit,
            points=points,
        )

    async def async_get_yearly_summary(self, premise_no: str) -> dict[str, Any]:
        """Fetch raw /jarvis/v4/charts/{premise_id} 12-month billed summary."""
        url = YEARLY_SUMMARY_URL.format(premise_id=quote_path_segment(premise_no))
        res = await self._request("GET", url)
        return res if isinstance(res, dict) else {}

    async def async_get_billed_history(
        self, premise_no: str
    ) -> tuple[str | None, dict[str, UtilityBilledSummary]]:
        """Fetch and parse 12-month billed usage + neighbour/efficient benchmarks for elec, water, and gas."""
        raw = await self.async_get_yearly_summary(premise_no)
        premise_type = raw.get("premise_type") if isinstance(raw.get("premise_type"), str) else None

        key_map = {
            "elec": ("electricity", "kWh"),
            "water": ("water", "m³"),
            "gas": ("gas", "kWh"),
        }
        summaries: dict[str, UtilityBilledSummary] = {}

        for api_key, (canon_key, fallback_unit) in key_map.items():
            util_obj = raw.get(api_key)
            if not isinstance(util_obj, dict):
                continue

            months: list[BilledMonthPoint] = []
            detected_unit = fallback_unit
            for item in util_obj.get("data") or []:
                if not isinstance(item, dict):
                    continue
                item_unit = normalize_unit(item.get("unit"), fallback_unit)
                if item.get("unit"):
                    detected_unit = item_unit
                cons = item.get("consumption") if isinstance(item.get("consumption"), dict) else {}
                peer = item.get("peer_comparison") if isinstance(item.get("peer_comparison"), dict) else {}
                period_str = str(item.get("period") or "")
                months.append(
                    BilledMonthPoint(
                        period=period_str,
                        period_date=parse_sp_date(period_str),
                        status=str(item.get("status") or ""),
                        unit=item_unit,
                        current=safe_float(cons.get("current"), 0.0) or 0.0,
                        previous=safe_float(cons.get("previous"), 0.0) or 0.0,
                        peer_average=safe_float(peer.get("average")),
                        peer_efficient=safe_float(peer.get("efficient")),
                    )
                )

            months.sort(key=lambda m: (m.period_date is None, m.period_date or m.period))
            summaries[canon_key] = UtilityBilledSummary(
                utility_key=canon_key,
                unit=detected_unit,
                average_consumption=safe_float(util_obj.get("average_consumption")),
                comparison_message=util_obj.get("comparison_message"),
                comparison_message_type=util_obj.get("comparison_message_type"),
                comparison_type=util_obj.get("comparison_type"),
                months=months,
            )

        return premise_type, summaries

    # -------------------------------------------------------------------------
    # Hardware Meter Readings & SMRD Self-Reading Schedule
    # -------------------------------------------------------------------------

    async def async_get_meter_readings(
        self, premises: list[Premise]
    ) -> dict[str, dict[str, MeterReadingDetail]]:
        """Fetch actual hardware meter registers & SMRD windows per account and utility."""
        results: dict[str, dict[str, MeterReadingDetail]] = {}
        account_nos = [p.account_no for p in premises if p.account_no]
        if not account_nos:
            return results

        acct_lookup = {account_digits(p.account_no): p.account_no for p in premises}

        payload = {"accounts": [{"accountNo": acct} for acct in account_nos]}
        data = await self._optional_request(
            "POST",
            METER_READ_DETAILS_URL,
            json=payload,
            timeout_seconds=HTTP_TIMEOUT_SECONDS,
        )
        if isinstance(data, dict):
            for item in data.get("meterReadDataItemList") or []:
                if not isinstance(item, dict):
                    continue
                raw_acct = str(item.get("accountNo") or "")
                canon_acct = acct_lookup.get(account_digits(raw_acct), raw_acct)
                util = _canonical_service_name(item.get("utilityType"))
                if not canon_acct or not util:
                    continue
                results.setdefault(canon_acct, {})[util] = MeterReadingDetail(
                    account_no=canon_acct,
                    utility_type=util,
                    device_no=item.get("deviceNo"),
                    last_actual_reading=safe_float(item.get("lastActualReading")),
                    last_actual_read_date=parse_sp_date(item.get("lastActualReadDate")),
                    last_self_submitted=safe_float(item.get("lastSelfSubmitted")),
                    last_self_submitted_date=parse_sp_date(
                        item.get("lastSelfSubmittedDate") or item.get("lastSelfReadDate")
                    ),
                    is_smrd_window=bool(item.get("isSmrdWindow", False)),
                    smrd_from_date=parse_sp_date(item.get("smrdFromDate")),
                    smrd_to_date=parse_sp_date(item.get("smrdToDate")),
                    next_mnr_date=parse_sp_date(item.get("nextMnr")),
                    next_mnr_from_date=parse_sp_date(item.get("nextMnrFromDate")),
                    next_mnr_to_date=parse_sp_date(item.get("nextMnrToDate")),
                    bill_month=item.get("billMonth"),
                )

        # Fallback per premise using Jarvis /jarvis/v3/smrd-uportal/{premise_id}
        for p in premises:
            if p.account_no in results or not p.premise_no:
                continue
            url = JARVIS_METER_READINGS_V2_URL.format(
                premise_id=quote_path_segment(p.premise_no)
            )
            jdata = await self._optional_request(
                "GET", url, timeout_seconds=HTTP_TIMEOUT_SECONDS
            )
            if not isinstance(jdata, dict):
                continue
            sub_period = (
                jdata.get("latest_submission_period")
                if isinstance(jdata.get("latest_submission_period"), dict)
                else {}
            )
            within_period = bool(sub_period.get("within_period", False))
            start_d = parse_sp_date(sub_period.get("start_date"))
            end_d = parse_sp_date(sub_period.get("end_date"))
            for m in jdata.get("meters") or []:
                if not isinstance(m, dict):
                    continue
                util = _canonical_service_name(m.get("utility_type"))
                if not util:
                    continue
                results.setdefault(p.account_no, {})[util] = MeterReadingDetail(
                    account_no=p.account_no,
                    utility_type=util,
                    device_no=m.get("meter_id"),
                    last_actual_reading=safe_float(m.get("last_actual_value")),
                    last_actual_read_date=parse_sp_date(m.get("last_actual_at")),
                    last_self_submitted=safe_float(m.get("last_submitted_value")),
                    last_self_submitted_date=parse_sp_date(m.get("last_submitted_at")),
                    is_smrd_window=within_period,
                    smrd_from_date=start_d,
                    smrd_to_date=end_d,
                    next_mnr_from_date=parse_sp_date(m.get("next_mnr_from_date")),
                    next_mnr_to_date=parse_sp_date(m.get("next_mnr_to_date")),
                )

        return results

    # -------------------------------------------------------------------------
    # Billing, Outstanding Payables, Bills PDF, Payments & PPMS Credit
    # -------------------------------------------------------------------------

    async def async_get_billing_summaries(
        self, premises: list[Premise]
    ) -> dict[str, AccountBillingSummary]:
        """Fetch payables, full bill history, payment transactions, e-Bill prefs, and PPMS balances."""
        summaries: dict[str, AccountBillingSummary] = {
            p.account_no: AccountBillingSummary(
                account_no=p.account_no, premise_no=p.premise_no
            )
            for p in premises
        }
        acct_lookup = {account_digits(p.account_no): p.account_no for p in premises}
        premise_lookup = {str(p.premise_no): p.account_no for p in premises if p.premise_no}

        payables_res, history_res, prefs_res = await asyncio.gather(
            self._optional_request("GET", PAYABLES_URL, timeout_seconds=HTTP_TIMEOUT_SECONDS),
            self._optional_request("GET", UTILITIES_HISTORY_URL, timeout_seconds=HTTP_TIMEOUT_SECONDS),
            self._optional_request("GET", BILL_PREFERENCES_URL),
            return_exceptions=True,
        )

        for res in (payables_res, history_res, prefs_res):
            if isinstance(res, AuthException):
                raise res

        # 1. Njord /njord/v4/payables
        if isinstance(payables_res, dict):
            for item in payables_res.get("payables") or []:
                if not isinstance(item, dict):
                    continue
                raw_acct = str(item.get("account_number") or "")
                canon_acct = acct_lookup.get(account_digits(raw_acct))
                if not canon_acct and item.get("premises_id"):
                    canon_acct = premise_lookup.get(str(item["premises_id"]))
                if not canon_acct and len(summaries) == 1:
                    canon_acct = next(iter(summaries))
                if not canon_acct or canon_acct not in summaries:
                    continue
                s = summaries[canon_acct]
                s.outstanding_amount_sgd = cents_to_dollars(item.get("amount"))
                if item.get("currency"):
                    s.currency = str(item["currency"]).strip().upper()
                if isinstance(item.get("giro_enabled"), bool):
                    s.giro_enabled = item["giro_enabled"]
                if isinstance(item.get("recurring_enabled"), bool):
                    s.recurring_enabled = item["recurring_enabled"]
                if isinstance(item.get("is_owner"), bool):
                    s.is_owner = item["is_owner"]
                if item.get("system"):
                    s.system = str(item["system"])

        # 2. Njord /njord/v3/history (sort bills by UTC instant, never lexicographic text!)
        if isinstance(history_res, dict):
            bills_by_acct: dict[str, list[tuple[datetime, BillRecord]]] = {}
            for entry in history_res.get("history") or []:
                if not isinstance(entry, dict):
                    continue
                bill = entry.get("bill")
                if isinstance(bill, dict):
                    raw_acct = str(bill.get("account_number") or "")
                    canon_acct = acct_lookup.get(account_digits(raw_acct))
                    if not canon_acct and len(summaries) == 1:
                        canon_acct = next(iter(summaries))
                    amt = cents_to_dollars(bill.get("amount"))
                    if canon_acct and canon_acct in summaries and amt is not None:
                        issued_dt = (
                            parse_sp_datetime(bill.get("date"))
                            or parse_sp_datetime(bill.get("period"))
                            or parse_sp_datetime(entry.get("created_at"))
                        )
                        rec = BillRecord(
                            account_no=canon_acct,
                            amount_sgd=amt,
                            bill_date=parse_sp_date(bill.get("date") or entry.get("created_at")),
                            due_date=parse_sp_date(bill.get("due_date")),
                            period=bill.get("period"),
                            pdf_url=bill.get("pdf_url"),
                            issued_at=issued_dt,
                        )
                        sort_key = (
                            issued_dt.astimezone(UTC)
                            if issued_dt is not None
                            else datetime.min.replace(tzinfo=UTC)
                        )
                        bills_by_acct.setdefault(canon_acct, []).append((sort_key, rec))

                txn = entry.get("transaction")
                if isinstance(txn, dict):
                    txn_accts = txn.get("account_numbers") or []
                    matched_accts = [
                        acct_lookup[account_digits(a)]
                        for a in txn_accts
                        if account_digits(a) in acct_lookup
                    ]
                    if not matched_accts and len(summaries) == 1:
                        matched_accts = list(summaries.keys())
                    txn_dt = parse_sp_datetime(txn.get("txn_time") or entry.get("created_at"))
                    for canon_acct in matched_accts:
                        s = summaries[canon_acct]
                        if s.latest_payment_amount_sgd is None or (
                            txn_dt
                            and (
                                s.latest_payment_date is None
                                or txn_dt.astimezone(UTC) > s.latest_payment_date.astimezone(UTC)
                            )
                        ):
                            s.latest_payment_amount_sgd = cents_to_dollars(txn.get("amount"))
                            s.latest_payment_date = txn_dt
                            s.latest_payment_method = txn.get("payment_method")
                            s.latest_payment_status = txn.get("status")
                            s.latest_payment_channel = txn.get("channel")
                            if isinstance(txn.get("is_recurring"), bool):
                                s.latest_payment_recurring = txn["is_recurring"]
                            s.latest_payment_txn_id = txn.get("txn_id")

            for canon_acct, bill_pairs in bills_by_acct.items():
                bill_pairs.sort(key=lambda pair: pair[0])
                ordered_bills = [rec for _, rec in bill_pairs]
                s = summaries[canon_acct]
                s.bills = ordered_bills
                if ordered_bills:
                    newest = ordered_bills[-1]
                    s.latest_bill_amount_sgd = newest.amount_sgd
                    s.latest_bill_date = newest.bill_date
                    s.latest_bill_due_date = newest.due_date
                    s.latest_bill_period = newest.period
                    s.latest_bill_pdf_url = newest.pdf_url

        # 3. Skalbox /skalbox/b2c/account/v1/retrieveBillPreferences
        if isinstance(prefs_res, dict):
            for pref in prefs_res.get("preferences") or []:
                if not isinstance(pref, dict):
                    continue
                raw_acct = str(pref.get("accountNo") or "")
                canon_acct = acct_lookup.get(account_digits(raw_acct))
                if not canon_acct and len(summaries) == 1:
                    canon_acct = next(iter(summaries))
                if canon_acct and canon_acct in summaries:
                    s = summaries[canon_acct]
                    if isinstance(pref.get("isSoftCopy"), bool):
                        s.ebill_enabled = pref["isSoftCopy"]
                    if isinstance(pref.get("isHardCopy"), bool):
                        s.paper_bill_enabled = pref["isHardCopy"]

        # 4. PPMS Prepaid Credit Balance (only for PPMS premises)
        for p in premises:
            if not p.ppms or not p.premise_no:
                continue
            bal = await self.async_get_ppms_balance(p.premise_no)
            if bal is not None:
                summaries[p.account_no].ppms_balance_sgd = bal[0]
                summaries[p.account_no].ppms_updated_at = bal[1]

        return summaries

    async def async_get_ppms_balance(
        self, premise_no: str
    ) -> tuple[float | None, str | None] | None:
        """Fetch Prepaid Metering System (PPMS) credit balance for a PPMS premise."""
        escaped = quote_path_segment(premise_no)
        for url_tpl in (NJORD_PPMS_BALANCE_URL, JARVIS_PPMS_BALANCE_URL):
            data = await self._optional_request(
                "GET",
                url_tpl.format(premise_id=escaped),
                timeout_seconds=HTTP_TIMEOUT_SECONDS,
            )
            if isinstance(data, dict) and data.get("amount") is not None:
                return round_sgd(safe_float(data.get("amount"))), data.get("updated_at")
        return None

    # -------------------------------------------------------------------------
    # Regulated Tariff, OEM Price Plans & Green Goals
    # -------------------------------------------------------------------------

    async def async_get_tariff_comparison(
        self, monthly_kwh: float = 350.0
    ) -> TariffComparisonSummary | None:
        """Fetch current SP regulated electricity & gas tariffs + PUB domestic water prices."""
        rounded_kwh = (
            round_half_up(Decimal(str(monthly_kwh)), "1")
            if monthly_kwh and monthly_kwh > 0
            else 350.0
        )
        ref_kwh = max(50, int(rounded_kwh or 350))

        summary: TariffComparisonSummary | None = None
        data: Any = None
        for candidate_url, use_auth in (
            (B2C_PRICE_PLAN_URL, True),
            (PRICE_PLAN_URL, False),
        ):
            res = await self._optional_request(
                "GET",
                candidate_url,
                authenticated=use_auth,
                record_failure=False,
                params={"consumption": str(ref_kwh)},
            )
            if isinstance(res, dict) and (
                safe_float(res.get("sp_kwh_price")) is not None
                or safe_float(res.get("sp_monthly_price")) is not None
            ):
                data = res
                break

        web_tariff, water_info = await asyncio.gather(
            self._async_fetch_spgroup_web_tariff(ref_kwh),
            self._async_fetch_singstat_water_price(),
        )

        if isinstance(data, dict):
            sp_kwh_price = safe_float(data.get("sp_kwh_price"))
            sp_monthly = safe_float(data.get("sp_monthly_price"))
            if sp_kwh_price is not None or sp_monthly is not None:
                plans = data.get("price_plans") or []

                best_fixed_rate: float | None = None
                best_fixed_retailer: str | None = None
                best_fixed_plan: str | None = None
                best_fixed_savings: float | None = None

                best_disc_pct: float | None = None
                best_disc_rate: float | None = None
                best_disc_retailer: str | None = None
                best_disc_plan: str | None = None

                for plan in plans if isinstance(plans, list) else []:
                    if not isinstance(plan, dict):
                        continue
                    retailer = plan.get("retailer_name")
                    plan_name = plan.get("plan_name")
                    psummary = (
                        plan.get("price_summary")
                        if isinstance(plan.get("price_summary"), dict)
                        else {}
                    )

                    fixed_obj = (
                        plan.get("fixed_price")
                        if isinstance(plan.get("fixed_price"), dict)
                        else {}
                    )
                    fixed_price = safe_float(fixed_obj.get("price"))
                    if fixed_price is not None and (
                        best_fixed_rate is None or fixed_price < best_fixed_rate
                    ):
                        best_fixed_rate = fixed_price
                        best_fixed_retailer = retailer
                        best_fixed_plan = plan_name
                        best_fixed_savings = safe_float(psummary.get("monthly_savings"))

                    disc_obj = (
                        plan.get("discount_off_tariff")
                        if isinstance(plan.get("discount_off_tariff"), dict)
                        else {}
                    )
                    disc_pct = safe_float(disc_obj.get("percent"))
                    disc_price = safe_float(disc_obj.get("price"))
                    if disc_pct is not None and (
                        best_disc_pct is None or disc_pct > best_disc_pct
                    ):
                        best_disc_pct = disc_pct
                        best_disc_rate = disc_price
                        best_disc_retailer = retailer
                        best_disc_plan = plan_name

                summary = TariffComparisonSummary(
                    sp_kwh_price=sp_kwh_price,
                    sp_monthly_price=sp_monthly,
                    reference_consumption_kwh=float(ref_kwh),
                    tariff_source="spdigital.sg",
                    best_fixed_rate=best_fixed_rate,
                    best_fixed_retailer=best_fixed_retailer,
                    best_fixed_plan=best_fixed_plan,
                    best_fixed_monthly_savings=best_fixed_savings,
                    best_discount_pct=best_disc_pct,
                    best_discount_rate=best_disc_rate,
                    best_discount_retailer=best_disc_retailer,
                    best_discount_plan=best_disc_plan,
                    total_oem_plans=len(plans) if isinstance(plans, list) else 0,
                )

        if summary is None:
            summary = web_tariff or await self._async_fetch_gov_regulated_tariff(ref_kwh)
        elif web_tariff is not None:
            if summary.sp_kwh_price_excl_gst is None:
                summary.sp_kwh_price_excl_gst = web_tariff.sp_kwh_price_excl_gst
            if summary.sp_gas_kwh_price is None:
                summary.sp_gas_kwh_price = web_tariff.sp_gas_kwh_price
                summary.sp_gas_kwh_price_excl_gst = web_tariff.sp_gas_kwh_price_excl_gst
                summary.gas_tariff_source = web_tariff.gas_tariff_source
            if summary.tariff_period is None:
                summary.tariff_period = web_tariff.tariff_period

        if summary is None and water_info:
            summary = TariffComparisonSummary(reference_consumption_kwh=float(ref_kwh))

        if summary is not None and summary.sp_gas_kwh_price is None:
            gas_fallback = await self._async_fetch_singstat_gas_tariff()
            if gas_fallback:
                summary.sp_gas_kwh_price = gas_fallback["incl_gst"]
                summary.sp_gas_kwh_price_excl_gst = gas_fallback["excl_gst"]
                summary.gas_tariff_source = "singstat.gov.sg"

        if summary is not None and water_info:
            summary.sp_water_m3_price = water_info.get("tier1_incl_gst")
            summary.sp_water_m3_price_excl_gst = water_info.get("tier1_excl_gst")
            summary.sp_water_above40_m3_price = water_info.get("tier2_incl_gst")
            summary.sp_water_above40_m3_price_excl_gst = water_info.get("tier2_excl_gst")
            summary.sp_water_base_tariff_excl_gst = water_info.get("t1_base")
            summary.sp_water_wct_excl_gst = water_info.get("t1_wct")
            summary.sp_water_wbt_excl_gst = water_info.get("t1_wbt")
            summary.sp_water_above40_base_tariff_excl_gst = water_info.get("t2_base")
            summary.sp_water_above40_wct_excl_gst = water_info.get("t2_wct")
            summary.sp_water_above40_wbt_excl_gst = water_info.get("t2_wbt")
            summary.water_tariff_year = water_info.get("year")
            summary.water_tariff_source = "singstat.gov.sg"

        return summary

    async def _async_fetch_spgroup_web_tariff(
        self, ref_kwh: int
    ) -> TariffComparisonSummary | None:
        """Fetch live current-quarter electricity & gas tariffs from SP Group's official tariff page."""
        cents_re = re.compile(r"(\d+(?:\.\d+)?)\s*cents\s*/\s*kwh", re.IGNORECASE)
        period_re = re.compile(r"\((wef[^)]+)\)", re.IGNORECASE)

        try:
            async with self._session.get(
                SPGROUP_TARIFF_PAGE_URL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant-SP/0.3)"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        return None
                html = bytes(buf).decode(JSON_ENCODING, errors="replace")
        except Exception as err:
            _LOGGER.debug("SP Group live tariff page unreachable: %s", safe_log_text(err))
            return None

        elec_incl: float | None = None
        elec_excl: float | None = None
        gas_incl: float | None = None
        gas_excl: float | None = None
        period_text: str | None = None

        next_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>',
            html,
            re.IGNORECASE,
        )
        if next_match:
            try:
                next_json = json.loads(next_match.group(1))
            except json.JSONDecodeError:
                next_json = None

            stack: list[Any] = [next_json] if next_json is not None else []
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    desc_raw = node.get("description")
                    text_raw = node.get("text")
                    if isinstance(desc_raw, str) and isinstance(text_raw, str):
                        folded_desc = fold_text(desc_raw)
                        t_match = cents_re.search(text_raw)
                        d_match = cents_re.search(desc_raw)
                        p_match = period_re.search(desc_raw)
                        if "electricity tariff" in folded_desc and t_match:
                            val_incl = safe_float(t_match.group(1))
                            if val_incl is not None and 5.0 <= val_incl <= 500.0:
                                elec_incl = round_half_up(
                                    Decimal(str(val_incl)) / Decimal("100"), "0.0001"
                                )
                            if d_match:
                                val_excl = safe_float(d_match.group(1))
                                if val_excl is not None and 5.0 <= val_excl <= 500.0:
                                    elec_excl = round_half_up(
                                        Decimal(str(val_excl)) / Decimal("100"), "0.0001"
                                    )
                            if p_match and not period_text:
                                period_text = " ".join(p_match.group(1).split())
                        elif "gas tariff" in folded_desc and t_match:
                            g_incl = safe_float(t_match.group(1))
                            if g_incl is not None and 5.0 <= g_incl <= 500.0:
                                gas_incl = round_half_up(
                                    Decimal(str(g_incl)) / Decimal("100"), "0.0001"
                                )
                            if d_match:
                                g_excl = safe_float(d_match.group(1))
                                if g_excl is not None and 5.0 <= g_excl <= 500.0:
                                    gas_excl = round_half_up(
                                        Decimal(str(g_excl)) / Decimal("100"), "0.0001"
                                    )
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)

        # Plain-HTML fallback if CMS switches from __NEXT_DATA__ to RSC/SSR markup
        if elec_incl is None:
            stripped = " ".join(re.sub(r"<[^>]+>", " ", html).split())
            html_elec = re.search(
                r"(\d+\.\d+)\s*cents/kWh\s+(?:(\d+\.\d+)\s*cents/kWh\s*\(w/o\s*GST\)\s*)?ELECTRICITY\s+TARIFF",
                stripped,
                re.IGNORECASE,
            )
            if html_elec:
                v_in = safe_float(html_elec.group(1))
                if v_in is not None and 5.0 <= v_in <= 500.0:
                    elec_incl = round_half_up(Decimal(str(v_in)) / Decimal("100"), "0.0001")
                if html_elec.group(2):
                    v_ex = safe_float(html_elec.group(2))
                    if v_ex is not None and 5.0 <= v_ex <= 500.0:
                        elec_excl = round_half_up(Decimal(str(v_ex)) / Decimal("100"), "0.0001")

        monthly_est = (
            round_sgd(Decimal(str(elec_incl)) * Decimal(ref_kwh))
            if elec_incl is not None
            else None
        )
        if elec_incl is None and gas_incl is None:
            return None
        return TariffComparisonSummary(
            sp_kwh_price=elec_incl,
            sp_kwh_price_excl_gst=elec_excl,
            sp_monthly_price=monthly_est,
            reference_consumption_kwh=float(ref_kwh),
            sp_gas_kwh_price=gas_incl,
            sp_gas_kwh_price_excl_gst=gas_excl,
            gas_tariff_source="spgroup.com.sg" if gas_incl is not None else None,
            tariff_period=period_text,
            tariff_source="spgroup.com.sg",
            total_oem_plans=0,
        )

    async def _async_fetch_singstat_gas_tariff(self) -> dict[str, float | None] | None:
        """Fallback to SingStat TableBuilder M891051 Piped Town Gas General Tariff (+9% GST)."""
        body = await self._optional_request(
            "GET",
            SINGSTAT_GAS_TARIFF_URL,
            authenticated=False,
            record_failure=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant-SP/0.3)"},
        )
        if not isinstance(body, dict):
            return None
        rows = (body.get("Data") or {}).get("row") or []
        cents_pre_gst: float | None = None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            label = fold_text(str(row.get("rowText") or ""))
            if "piped town gas" in label and "general tariff" in label:
                cols = row.get("columns") or []
                for col in reversed(cols if isinstance(cols, list) else []):
                    if isinstance(col, dict):
                        val = safe_float(col.get("value"))
                        if val is not None and val > 0:
                            cents_pre_gst = val
                            break
                break
        if cents_pre_gst is None:
            return None
        excl = round_half_up(Decimal(str(cents_pre_gst)) / Decimal("100"), "0.0001")
        incl = round_half_up(
            (Decimal(str(cents_pre_gst)) / Decimal("100")) * Decimal(SG_GST_MULTIPLIER),
            "0.0001",
        )
        if incl is None:
            return None
        return {"incl_gst": incl, "excl_gst": excl}

    async def _async_fetch_singstat_water_price(self) -> dict[str, Any] | None:
        """Fetch official PUB all-in domestic potable water prices (Tariff + WCT + WBT) from SingStat M890981."""
        body = await self._optional_request(
            "GET",
            SINGSTAT_WATER_PRICE_URL,
            authenticated=False,
            record_failure=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant-SP/0.3)"},
        )
        if not isinstance(body, dict):
            return None

        def _latest_col(row_obj: dict[str, Any]) -> tuple[float | None, str | None]:
            cols = row_obj.get("columns") or []
            for col in reversed(cols if isinstance(cols, list) else []):
                if isinstance(col, dict):
                    val = safe_float(col.get("value"))
                    if val is not None and val > 0:
                        key_str = str(col.get("key")).strip() if col.get("key") else None
                        return val, key_str
            return None, None

        rows = (body.get("Data") or {}).get("row") or []
        section: str | None = None
        tier1_excl: float | None = None
        tier2_excl: float | None = None
        year_key: str | None = None
        t1_base: float | None = None
        t1_wct: float | None = None
        t1_wbt: float | None = None
        t2_base: float | None = None
        t2_wct: float | None = None
        t2_wbt: float | None = None

        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            label = fold_text(str(row.get("rowText") or ""))
            if "potable water price for domestic users" in label:
                if "0-40" in label or "0 - 40" in label:
                    section = "tier1"
                    tier1_excl, year_key = _latest_col(row)
                else:
                    section = "tier2"
                    tier2_excl, _ = _latest_col(row)
            elif any(
                k in label
                for k in ("non-domestic", "shipping", "newater", "industrial")
            ):
                section = "other"
            elif section == "tier1":
                if label == "tariff":
                    t1_base, _ = _latest_col(row)
                elif "water conservation tax" in label:
                    t1_wct, _ = _latest_col(row)
                elif "waterborne" in label:
                    t1_wbt, _ = _latest_col(row)
            elif section == "tier2":
                if label == "tariff":
                    t2_base, _ = _latest_col(row)
                elif "water conservation tax" in label:
                    t2_wct, _ = _latest_col(row)
                elif "waterborne" in label:
                    t2_wbt, _ = _latest_col(row)

        if tier1_excl is None:
            return None

        tier1_incl = round_half_up(
            Decimal(str(tier1_excl)) * Decimal(SG_GST_MULTIPLIER), "0.0001"
        )
        tier2_incl = (
            round_half_up(
                Decimal(str(tier2_excl)) * Decimal(SG_GST_MULTIPLIER), "0.0001"
            )
            if tier2_excl is not None
            else None
        )
        if tier1_incl is None:
            return None

        return {
            "tier1_incl_gst": tier1_incl,
            "tier1_excl_gst": tier1_excl,
            "tier2_incl_gst": tier2_incl,
            "tier2_excl_gst": tier2_excl,
            "t1_base": t1_base,
            "t1_wct": t1_wct,
            "t1_wbt": t1_wbt,
            "t2_base": t2_base,
            "t2_wct": t2_wct,
            "t2_wbt": t2_wbt,
            "year": year_key,
        }

    async def _async_fetch_gov_regulated_tariff(
        self, ref_kwh: int
    ) -> TariffComparisonSummary | None:
        """Fallback to official Singapore SingStat (M890991) / data.gov.sg SP domestic tariff (+9% GST)."""
        cents_pre_gst: float | None = None
        gov_headers = {"User-Agent": "Mozilla/5.0 (compatible; HomeAssistant-SP/0.3)"}

        # 1. SingStat TableBuilder M890991 (chronological columns, newest at end)
        ss_body = await self._optional_request(
            "GET",
            SINGSTAT_TARIFF_URL,
            authenticated=False,
            record_failure=False,
            headers=gov_headers,
        )
        if isinstance(ss_body, dict):
            rows = (ss_body.get("Data") or {}).get("row") or []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                label = fold_text(str(row.get("rowText") or ""))
                if "low tension" in label and "domestic" in label and "non-domestic" not in label:
                    cols = row.get("columns") or []
                    for col in reversed(cols if isinstance(cols, list) else []):
                        if isinstance(col, dict):
                            val = safe_float(col.get("value"))
                            if val is not None and val > 0:
                                cents_pre_gst = val
                                break
                    if cents_pre_gst is not None:
                        break

        # 2. data.gov.sg datastore fallback (d_61eac3cdb086814af485dcc682b75ae9)
        if cents_pre_gst is None:
            dg_body = await self._optional_request(
                "GET",
                DATAGOV_TARIFF_URL,
                authenticated=False,
                record_failure=False,
                headers=gov_headers,
            )
            if isinstance(dg_body, dict):
                records = (dg_body.get("result") or {}).get("records") or []
                month_idx = {
                    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                }
                for rec in records if isinstance(records, list) else []:
                    if not isinstance(rec, dict):
                        continue
                    series = fold_text(str(rec.get("DataSeries") or ""))
                    if "low tension" in series and "domestic" in series and "non-domestic" not in series:
                        dated_vals: list[tuple[int, int, float]] = []
                        for k, v in rec.items():
                            if len(k) == 7 and k[:4].isdigit():
                                m_num = month_idx.get(k[4:].lower())
                                fval = safe_float(v)
                                if m_num and fval is not None and fval > 0:
                                    dated_vals.append((int(k[:4]), m_num, fval))
                        if dated_vals:
                            dated_vals.sort()
                            cents_pre_gst = dated_vals[-1][2]
                        break

        if cents_pre_gst is None:
            return None

        kwh_incl_gst = round_half_up(
            (Decimal(str(cents_pre_gst)) / Decimal("100")) * Decimal(SG_GST_MULTIPLIER),
            "0.0001",
        )
        if kwh_incl_gst is None:
            return None
        monthly_est = round_sgd(Decimal(str(kwh_incl_gst)) * Decimal(ref_kwh))
        return TariffComparisonSummary(
            sp_kwh_price=kwh_incl_gst,
            sp_kwh_price_excl_gst=round_half_up(
                Decimal(str(cents_pre_gst)) / Decimal("100"), "0.0001"
            ),
            sp_monthly_price=monthly_est,
            reference_consumption_kwh=float(ref_kwh),
            tariff_source="singstat.gov.sg",
            total_oem_plans=0,
        )

    async def async_get_green_goals(
        self, premises: list[Premise]
    ) -> dict[str, dict[str, GreenGoalSummary]]:
        """Fetch /jarvis/v5/greengoals/targets sustainability goals per premise."""
        data = await self._optional_request("GET", GREEN_GOALS_URL)
        results: dict[str, dict[str, GreenGoalSummary]] = {}
        if not isinstance(data, dict):
            return results

        premise_lookup = {str(p.premise_no): p.account_no for p in premises if p.premise_no}
        for goal in data.get("goals") or []:
            if not isinstance(goal, dict):
                continue
            util = _canonical_service_name(goal.get("type"))
            if not util:
                continue
            default_u = "kWh" if util == "electricity" else "m³"
            unit = normalize_unit(goal.get("unit"), default_u)
            month = goal.get("month")
            for gp in goal.get("premises") or []:
                if not isinstance(gp, dict):
                    continue
                pid = str(gp.get("id") or "")
                acct_no = premise_lookup.get(pid)
                if not acct_no and len(premises) == 1:
                    acct_no = premises[0].account_no
                if not acct_no:
                    continue
                target_data = gp.get("data") if isinstance(gp.get("data"), dict) else {}
                used = safe_float(target_data.get("used"))
                target = safe_float(target_data.get("target"))
                diff_pct = safe_float(gp.get("consumption_percentage_difference"))
                raw_cost_cents = (
                    gp.get("cost_difference_in_cents")
                    if gp.get("cost_difference_in_cents") is not None
                    else gp.get("cost_difference")
                )
                if (used or 0.0) == 0.0 and (target or 0.0) == 0.0 and diff_pct is None:
                    continue
                results.setdefault(acct_no, {})[util] = GreenGoalSummary(
                    utility_type=util,
                    month=month,
                    unit=unit,
                    used=used,
                    target=target,
                    consumption_diff_pct=diff_pct,
                    cost_diff_sgd=cents_to_dollars(raw_cost_cents),
                )

        return results

    # -------------------------------------------------------------------------
    # Optional Ecosystem Services: GreenUP, EV Charging, Tengah FCUs & Alerts
    # -------------------------------------------------------------------------

    async def async_get_greenup_summary(self) -> GreenUpSummary | None:
        """Fetch SP GreenUP loyalty account leaves and tier from /1up/authenticated/graphql."""
        data = await self._optional_request(
            "POST",
            ONEUP_AUTH_GRAPHQL_URL,
            json={"query": QUERY_GREENUP_ACCOUNT_V2},
        )
        if not isinstance(data, dict):
            return None
        node = (((data.get("data") or {}).get("account") or {}).get("node")) or {}
        if not isinstance(node, dict) or safe_float(node.get("totalPoints")) is None:
            return None
        tier_node = ((node.get("tier") or {}).get("node")) or {}
        if not isinstance(tier_node, dict):
            tier_node = {}
        lvl = safe_float(tier_node.get("level"))
        pts_up = safe_float(tier_node.get("pointsToLevelUp"))
        safe_pts = safe_float(tier_node.get("safePoints"))
        return GreenUpSummary(
            total_points=int(safe_float(node.get("totalPoints")) or 0),
            projected_level_status=node.get("projectedLevelStatus"),
            tier_level=int(lvl) if lvl is not None else None,
            tier_name=tier_node.get("name"),
            tier_description=tier_node.get("description"),
            points_to_level_up=int(pts_up) if pts_up is not None else None,
            safe_points=int(safe_pts) if safe_pts is not None else None,
        )

    async def async_get_ev_wallet(self) -> EvWalletSummary | None:
        """Fetch SP EV loyalty wallet from /tyche/v1/wallet-summary."""
        body = await self._optional_request("GET", TYCHE_WALLET_URL)
        if not isinstance(body, dict):
            return None
        points = safe_float(body.get("points_balance"))
        dollars = safe_float(body.get("dollar_balance"))
        if points is None and dollars is None:
            return None
        if (points or 0.0) == 0.0 and (dollars or 0.0) == 0.0:
            return None
        tier = safe_float(body.get("current_tier_id"))
        return EvWalletSummary(
            points=points or 0.0,
            dollar_balance=round_sgd(dollars) if dollars is not None else None,
            current_tier_id=int(tier) if tier is not None else None,
        )

    async def async_get_ev_charging(
        self,
    ) -> tuple[EvSessionSummary | None, EvChargeSummary | None, EvUnpaidSummary | None]:
        """Fetch EV latest session, latest charge receipt, and unpaid orders (skipping on scope denial)."""
        try:
            session_body = await self._request(
                "GET",
                EVA_LATEST_SESSION_URL,
                timeout_seconds=OPTIONAL_HTTP_TIMEOUT_SECONDS,
            )
        except AuthException:
            raise
        except ApiException as err:
            if err.status_code == 404 or "Scope not granted" in str(err):
                return None, None, None
            self._note_read_failure("GET", EVA_LATEST_SESSION_URL, str(err))
            return None, None, None

        ev_session: EvSessionSummary | None = None
        if isinstance(session_body, dict):
            smap = (
                session_body["data"]
                if isinstance(session_body.get("data"), dict)
                else session_body
            )
            status = smap.get("status") if isinstance(smap.get("status"), str) and smap.get("status") else None
            kwh = safe_float(smap.get("kwh"))
            order_id = smap.get("order_id") if isinstance(smap.get("order_id"), str) and smap.get("order_id") else None
            if status is not None or kwh is not None or order_id is not None:
                ev_session = EvSessionSummary(
                    status=status,
                    kwh=kwh,
                    total_cost=str(smap["total_cost"]) if smap.get("total_cost") else None,
                    start=str(smap["start_datetime"]) if smap.get("start_datetime") else None,
                    order_id=order_id,
                )

        receipts_body, unpaid_body = await asyncio.gather(
            self._optional_request(
                "GET", EVA_CHARGE_HISTORY_URL, params={"offSet": "0", "pageSize": "5"}
            ),
            self._optional_request("GET", EVA_UNPAID_URL),
            return_exceptions=True,
        )

        ev_charge: EvChargeSummary | None = None
        if isinstance(receipts_body, dict):
            rows = receipts_body.get("data")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                first = rows[0]
                ckwh = safe_float(first.get("total_consumption"))
                if ckwh is None:
                    ckwh = safe_float(first.get("connector_kwh"))
                camt = parse_eva_sgd(first.get("transaction_amount"))
                if ckwh is not None or camt is not None:
                    ev_charge = EvChargeSummary(
                        kwh=ckwh,
                        amount_sgd=camt,
                        created_at=first.get("created_at") if isinstance(first.get("created_at"), str) else None,
                        status=first.get("transaction_status") if isinstance(first.get("transaction_status"), str) else None,
                        address=first.get("address") if isinstance(first.get("address"), str) else None,
                    )

        ev_unpaid: EvUnpaidSummary | None = None
        if isinstance(unpaid_body, dict):
            payload = (
                unpaid_body["data"]
                if isinstance(unpaid_body.get("data"), dict)
                else unpaid_body
            )
            orders = [
                r for r in (payload.get("orders") or []) if isinstance(r, dict)
            ]
            if orders:
                total_cents = 0
                found_amt = False
                for r in orders:
                    oa = parse_eva_sgd(r.get("amount"))
                    if oa is not None:
                        total_cents += round(oa * 100)
                        found_amt = True
                ev_unpaid = EvUnpaidSummary(
                    count=len(orders),
                    amount_sgd=round_sgd(Decimal(total_cents) / 100) if found_amt else None,
                )

        return ev_session, ev_charge, ev_unpaid

    async def async_get_unread_notifications(self) -> int | None:
        """Fetch unread notification count without storing notification message bodies."""
        body = await self._optional_request(
            "GET",
            NOTIFICATIONS_URL,
            params={
                "limit": "1",
                "include_totals_unread_notifications": "true",
                "include_notifications": "false",
            },
        )
        if not isinstance(body, dict):
            return None
        val = body.get("total_unread_notifications")
        if isinstance(val, bool) or not isinstance(val, (int, str)) or val == "":
            return None
        try:
            return int(val)
        except ValueError:
            return None

    async def async_get_optional_ecosystem(self) -> OptionalEcosystemData:
        """Concurrently fetch account-wide optional services (GreenUP, EV wallet/charging, notifications)."""
        greenup, ev_wallet, ev_tuple, unread = await asyncio.gather(
            self.async_get_greenup_summary(),
            self.async_get_ev_wallet(),
            self.async_get_ev_charging(),
            self.async_get_unread_notifications(),
            return_exceptions=True,
        )
        for item in (greenup, ev_wallet, ev_tuple, unread):
            if isinstance(item, AuthException):
                raise item

        ev_session, ev_charge, ev_unpaid = (
            ev_tuple if isinstance(ev_tuple, tuple) else (None, None, None)
        )
        return OptionalEcosystemData(
            greenup=greenup if isinstance(greenup, GreenUpSummary) else None,
            ev_wallet=ev_wallet if isinstance(ev_wallet, EvWalletSummary) else None,
            ev_session=ev_session,
            ev_last_charge=ev_charge,
            ev_unpaid=ev_unpaid,
            unread_notifications=unread if isinstance(unread, int) else None,
        )

    async def async_get_paired_fcus(self, account_no: str) -> list[FcuSummary]:
        """Fetch paired Tengah Fan Coil Units (FCUs) and their live status (capped at 8 coils)."""
        if not account_no:
            return []
        body = await self._optional_request(
            "POST",
            FROSTY_GRAPHQL_URL,
            json={
                "query": TENGAH_PAIRED_FCUS_QUERY,
                "variables": {"utilityAccountNumber": account_no},
            },
        )
        if not isinstance(body, dict):
            return []
        rows = (body.get("data") or {}).get("getPairedFCUs")
        if not isinstance(rows, list) or not rows:
            return []

        paired: list[tuple[str, str | None]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            thing = row.get("thingName")
            if isinstance(thing, str) and thing.strip():
                disp = row.get("displayName") if isinstance(row.get("displayName"), str) else None
                paired.append((thing.strip(), disp))

        if not paired:
            return []
        if len(paired) > MAX_FCU_STATUS_READS:
            _LOGGER.warning(
                "Upstream listed %d paired FCUs; reading the first %d",
                len(paired),
                MAX_FCU_STATUS_READS,
            )
            paired = paired[:MAX_FCU_STATUS_READS]

        statuses = await asyncio.gather(
            *(
                self._optional_request(
                    "GET",
                    FROSTY_FCU_STATUS_URL,
                    params={"thingName": thing, "utility_acc_id": account_no},
                )
                for thing, _ in paired
            ),
            return_exceptions=True,
        )

        fcus: list[FcuSummary] = []
        for (thing, disp), st in zip(paired, statuses, strict=True):
            if isinstance(st, AuthException):
                raise st
            if not isinstance(st, dict) or st.get("fcu_not_paired") is True:
                continue
            fcus.append(
                FcuSummary(
                    thing_name=thing,
                    display_name=disp or (st.get("display_name") if isinstance(st.get("display_name"), str) else None),
                    is_on=st.get("is_on") if isinstance(st.get("is_on"), bool) else None,
                    is_online=st.get("is_online") if isinstance(st.get("is_online"), bool) else None,
                    room_temperature=safe_float(st.get("room_temperature")),
                    setpoint=safe_float(st.get("temperature")),
                    mode=st.get("operation_mode") if isinstance(st.get("operation_mode"), str) else None,
                )
            )
        return fcus
