"""Data models for the pysingaporepower library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .utils import round_sgd


@dataclass
class AuthenticatorOption:
    """Represents an Auth0 MFA authenticator enrolled on the user's SP account."""

    authenticator_id: str
    auth_type: str  # "oob" or "otp"
    oob_channel: str | None = None  # "sms" or "email"
    name: str | None = None
    active: bool = True


@dataclass
class LoginChallengeResult:
    """Result of the initial password login step."""

    authenticated: bool
    mfa_required: bool = False
    selected_method: str | None = None  # "otp", "oob_sms", or "oob_email"
    available_methods: list[str] = field(default_factory=list)
    authenticators: list[AuthenticatorOption] = field(default_factory=list)
    masked_destination: str | None = None


@dataclass
class Premise:
    """Represents a single Singapore Power premise and its linked utility account."""

    account_no: str
    premise_no: str
    active_services: list[str] = field(default_factory=list)
    address: str | None = None
    postal_code: str | None = None
    premise_type: str | None = None
    contestable_type: str | None = None
    retailer_name: str | None = None
    retailer_id: str | None = None
    account_status: str | None = None
    account_type: str | None = None
    oem_eligible: bool | None = None
    ppms: bool = False
    move_in_date: str | None = None
    move_out_date: str | None = None
    ami_utilities: list[str] = field(default_factory=list)
    interval_utilities: list[str] = field(default_factory=list)
    ebs_account_no: str | None = None
    mssl_account_no: str | None = None
    bp_no: str | None = None
    system: str | None = None
    is_owner: bool = True


@dataclass
class Consumption:
    """Represents a single consumption interval/day/month data point."""

    date: datetime
    value: float
    amount_sgd: float | None = None
    anomaly: str | None = None
    tags: str | None = None
    raw_date: str | None = None


@dataclass
class AmiChartSummary:
    """Parsed response from /jarvis/v3/ami/charts or /frosty/consumption."""

    utility_type: str
    grouped_by: str
    unit: str
    daily_limit: float | None = None
    points: list[Consumption] = field(default_factory=list)

    @property
    def total_consumption(self) -> float:
        """Sum of consumption across all returned data points."""
        return round(sum(p.value for p in self.points), 4)

    @property
    def total_cost_sgd(self) -> float | None:
        """Sum of SGD cost across all returned data points if cost is provided."""
        costs = [p.amount_sgd for p in self.points if p.amount_sgd is not None]
        if not costs:
            return None
        return round_sgd(sum(costs))

    @property
    def latest_point(self) -> Consumption | None:
        """Return the most recent published data point."""
        if not self.points:
            return None
        return max(self.points, key=lambda p: p.date)

    @property
    def anomalies(self) -> list[dict[str, str]]:
        """Return any smart-meter anomalies flagged in metadata."""
        return [
            {
                "date": p.date.isoformat(),
                "anomaly": p.anomaly,
                "tags": p.tags or "",
            }
            for p in self.points
            if p.anomaly
        ]


@dataclass
class BilledMonthPoint:
    """Represents one billed month in /jarvis/v4/charts/{premise_id}."""

    period: str
    period_date: date | None
    status: str
    unit: str
    current: float
    previous: float
    peer_average: float | None = None
    peer_efficient: float | None = None


@dataclass
class UtilityBilledSummary:
    """Billed monthly history and peer comparison for one utility (elec, water, or gas)."""

    utility_key: str
    unit: str
    average_consumption: float | None = None
    comparison_message: str | None = None
    comparison_message_type: str | None = None
    comparison_type: str | None = None
    months: list[BilledMonthPoint] = field(default_factory=list)

    @property
    def latest_month(self) -> BilledMonthPoint | None:
        """Return the most recent non-filler billed month."""
        valid = [
            m
            for m in self.months
            if m.status.lower() != "filler" and (m.current > 0 or m.status)
        ]
        if not valid:
            return None
        with_dates = [m for m in valid if m.period_date is not None]
        if with_dates:
            return max(with_dates, key=lambda m: m.period_date)  # type: ignore[arg-type]
        return valid[-1]


@dataclass
class MeterReadingDetail:
    """Hardware meter reading and SMRD submission window details for a utility meter."""

    account_no: str
    utility_type: str
    device_no: str | None = None
    last_actual_reading: float | None = None
    last_actual_read_date: date | None = None
    last_self_submitted: float | None = None
    last_self_submitted_date: date | None = None
    is_smrd_window: bool = False
    smrd_from_date: date | None = None
    smrd_to_date: date | None = None
    next_mnr_date: date | None = None
    next_mnr_from_date: date | None = None
    next_mnr_to_date: date | None = None
    bill_month: str | None = None


@dataclass
class BillRecord:
    """A single utility bill from Njord /njord/v3/history ordered by issued_at instant."""

    account_no: str
    amount_sgd: float
    bill_date: date | None = None
    due_date: date | None = None
    period: str | None = None
    pdf_url: str | None = None
    issued_at: datetime | None = None


@dataclass
class AccountBillingSummary:
    """Billing, outstanding payable, bill list, payment history, e-Bill, and PPMS balance."""

    account_no: str
    premise_no: str | None = None
    outstanding_amount_sgd: float | None = None
    currency: str = "SGD"
    giro_enabled: bool | None = None
    recurring_enabled: bool | None = None
    is_owner: bool | None = None
    system: str | None = None
    ebill_enabled: bool | None = None
    paper_bill_enabled: bool | None = None
    bills: list[BillRecord] = field(default_factory=list)
    latest_bill_amount_sgd: float | None = None
    latest_bill_date: date | None = None
    latest_bill_due_date: date | None = None
    latest_bill_period: str | None = None
    latest_bill_pdf_url: str | None = None
    latest_payment_amount_sgd: float | None = None
    latest_payment_date: datetime | None = None
    latest_payment_method: str | None = None
    latest_payment_status: str | None = None
    latest_payment_channel: str | None = None
    latest_payment_recurring: bool | None = None
    latest_payment_txn_id: str | None = None
    ppms_balance_sgd: float | None = None
    ppms_updated_at: str | None = None


@dataclass
class GreenGoalSummary:
    """Sustainability Green Goal progress for a premise & utility (electric or water)."""

    utility_type: str
    month: str | None = None
    unit: str | None = None
    used: float | None = None
    target: float | None = None
    consumption_diff_pct: float | None = None
    cost_diff_sgd: float | None = None


@dataclass
class TariffComparisonSummary:
    """SP regulated electricity, gas, and PUB water tariffs + OEM plan comparison."""

    sp_kwh_price: float | None = None
    sp_kwh_price_excl_gst: float | None = None
    sp_monthly_price: float | None = None
    reference_consumption_kwh: float | None = None
    sp_gas_kwh_price: float | None = None
    sp_gas_kwh_price_excl_gst: float | None = None
    gas_tariff_source: str | None = None
    sp_water_m3_price: float | None = None
    sp_water_m3_price_excl_gst: float | None = None
    sp_water_above40_m3_price: float | None = None
    sp_water_above40_m3_price_excl_gst: float | None = None
    sp_water_base_tariff_excl_gst: float | None = None
    sp_water_wct_excl_gst: float | None = None
    sp_water_wbt_excl_gst: float | None = None
    sp_water_above40_base_tariff_excl_gst: float | None = None
    sp_water_above40_wct_excl_gst: float | None = None
    sp_water_above40_wbt_excl_gst: float | None = None
    water_tariff_year: str | None = None
    water_tariff_source: str | None = None
    tariff_period: str | None = None
    tariff_source: str | None = None
    best_fixed_rate: float | None = None
    best_fixed_retailer: str | None = None
    best_fixed_plan: str | None = None
    best_fixed_monthly_savings: float | None = None
    best_discount_pct: float | None = None
    best_discount_rate: float | None = None
    best_discount_retailer: str | None = None
    best_discount_plan: str | None = None
    total_oem_plans: int = 0


@dataclass
class GreenUpSummary:
    """SP GreenUP loyalty program summary."""

    total_points: int
    projected_level_status: str | None = None
    tier_level: int | None = None
    tier_name: str | None = None
    tier_description: str | None = None
    points_to_level_up: int | None = None
    safe_points: int | None = None


@dataclass
class EvWalletSummary:
    """SP EV loyalty wallet summary from /tyche/v1/wallet-summary."""

    points: float
    dollar_balance: float | None = None
    current_tier_id: int | None = None


@dataclass
class EvSessionSummary:
    """Latest SP EV charging session from /eva/v1/sessions/latest."""

    status: str | None = None
    kwh: float | None = None
    total_cost: str | None = None
    start: str | None = None
    order_id: str | None = None


@dataclass
class EvChargeSummary:
    """Latest completed SP EV charge receipt from /eva/v2/order/receipts."""

    kwh: float | None = None
    amount_sgd: float | None = None
    created_at: str | None = None
    status: str | None = None
    address: str | None = None


@dataclass
class EvUnpaidSummary:
    """Unpaid SP EV charging orders from /eva/v1/order/unpaid."""

    count: int
    amount_sgd: float | None = None


@dataclass
class FcuSummary:
    """Tengah centralized cooling Fan Coil Unit (FCU) status from Frosty."""

    thing_name: str
    display_name: str | None = None
    is_on: bool | None = None
    is_online: bool | None = None
    room_temperature: float | None = None
    setpoint: float | None = None
    mode: str | None = None


@dataclass
class OptionalEcosystemData:
    """Account-wide optional SP ecosystem services (GreenUP, EV charging, notifications)."""

    greenup: GreenUpSummary | None = None
    ev_wallet: EvWalletSummary | None = None
    ev_session: EvSessionSummary | None = None
    ev_last_charge: EvChargeSummary | None = None
    ev_unpaid: EvUnpaidSummary | None = None
    unread_notifications: int | None = None
