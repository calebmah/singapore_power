"""Constants for the pysingaporepower library (SP App v15.13.1 + live BFF contracts)."""

# Base URLs
B2C_BASE_URL = "https://b2c.api.spdigital.sg"
PUBLIC_BASE_URL = "https://public.api.spdigital.sg"
AUTH0_BASE_URL = "https://identity.spdigital.sg"
AUTH0_MFA_OAUTH_HOST = "https://identity.spdigital.auth0.com"

# Auth0 OAuth & MFA endpoints
OAUTH_TOKEN_URL = f"{AUTH0_BASE_URL}/oauth/token"
MFA_AUTHENTICATORS_URL = f"{AUTH0_BASE_URL}/mfa/authenticators"
MFA_CHALLENGE_URL = f"{AUTH0_BASE_URL}/mfa/challenge"
MFA_FALLBACK_AUTHENTICATORS_URL = f"{AUTH0_MFA_OAUTH_HOST}/mfa/authenticators"
MFA_FALLBACK_CHALLENGE_URL = f"{AUTH0_MFA_OAUTH_HOST}/mfa/challenge"

# Auth0 static client parameters (from Infinity/15.13.1 APK)
CLIENT_ID = "z1tl6I1V6HI201ule9tmSALb97hw8Biu"
SCOPE = (
    "openid email profile offline_access enroll read:authenticators "
    "user_metadata me me:uportal me:eva me:rbac"
)
REALM = "Username-Password-Authentication"
AUDIENCE = "https://profile.up.spdigital.sg/"

GRANT_TYPE_PASSWORD = "http://auth0.com/oauth/grant-type/password-realm"
GRANT_TYPE_MFA_OOB = "http://auth0.com/oauth/grant-type/mfa-oob"
GRANT_TYPE_MFA_OTP = "http://auth0.com/oauth/grant-type/mfa-otp"
GRANT_TYPE_REFRESH_TOKEN = "refresh_token"

# Auth0 / BFF protocol error codes
OAUTH_ERROR_MFA_REQUIRED = "mfa_required"
OAUTH_ERROR_REQUIRES_VERIFICATION = "requires_verification"
OAUTH_ERROR_TOO_MANY_ATTEMPTS = "too_many_attempts"
SCOPE_NOT_FOUND = "scope_not_found"

# App headers & encoding
USER_AGENT = "Infinity/15.13.1 (Android)"
JSON_ENCODING = "utf-8-sig"

# Timeouts, bounds & cooldowns
HTTP_TIMEOUT_SECONDS = 30
OPTIONAL_HTTP_TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_FCU_STATUS_READS = 8
LOGIN_RETRY_COOLDOWN_SECONDS = 60
MAX_USERNAME_OCTETS = 254
ERROR_VALUE_CHARS = 60
TRANSPORT_ERROR_CHARS = 160

# AMI backfill windows
AMI_HALF_HOUR_DAYS = 31
AMI_DAILY_MONTHS = 13

# Yggdrasil (Account & Premise GraphQL)
GRAPHQL_URL = f"{B2C_BASE_URL}/yggdrasil/graphql"

# Jarvis (Utilities, AMI Smart Meter Charts, Billed History, SMRD, Green Goals)
UTILITY_ACCOUNT_URL = f"{B2C_BASE_URL}/jarvis/v3/me"
CONSUMPTION_URL = f"{B2C_BASE_URL}/jarvis/v3/ami/charts"
PPMS_CHARTS_URL = f"{B2C_BASE_URL}/jarvis/v3/ppms/charts"
YEARLY_SUMMARY_URL = f"{B2C_BASE_URL}/jarvis/v4/charts/{{premise_id}}"
JARVIS_METER_READINGS_V2_URL = f"{B2C_BASE_URL}/jarvis/v3/smrd-uportal/{{premise_id}}"
JARVIS_PPMS_BALANCE_URL = f"{B2C_BASE_URL}/jarvis/v3/ppms/balance/{{premise_id}}"
GREEN_GOALS_URL = f"{B2C_BASE_URL}/jarvis/v5/greengoals/targets"

# Frosty (Tengah Centralized Cooling & Paired Fan Coil Units)
FROSTY_CONSUMPTION_URL = f"{B2C_BASE_URL}/frosty/consumption"
FROSTY_GRAPHQL_URL = f"{B2C_BASE_URL}/frosty/graphql"
FROSTY_FCU_STATUS_URL = f"{B2C_BASE_URL}/frosty/fcu_status"

# Njord (Payables, Bills, Payment Transactions, PPMS Balance)
PAYABLES_URL = f"{B2C_BASE_URL}/njord/v4/payables"
UTILITIES_HISTORY_URL = f"{B2C_BASE_URL}/njord/v3/history"
NJORD_PPMS_BALANCE_URL = f"{B2C_BASE_URL}/njord/v1/ppms/premises/{{premise_id}}/balance"

# Skalbox (Meter Reading Details, Cumulative Meter History, Bill Preferences)
METER_READ_DETAILS_URL = f"{B2C_BASE_URL}/skalbox/b2c/smrd/v1/retrieveMeterReadDetails"
METER_READ_HISTORY_URL = f"{B2C_BASE_URL}/skalbox/b2c/smrd/v1/retrieveMeterReadingHistory"
BILL_PREFERENCES_URL = f"{B2C_BASE_URL}/skalbox/b2c/account/v1/retrieveBillPreferences"

# PricePlan (SP Regulated Tariff & Open Electricity Market Retailer Plans - Public)
PRICE_PLAN_URL = f"{PUBLIC_BASE_URL}/priceplan/v2/plans/price"

# OneUp, Tyche, Eva & Notifications (Loyalty, EV Charging & In-App Alerts)
ONEUP_AUTH_GRAPHQL_URL = f"{B2C_BASE_URL}/1up/authenticated/graphql"
TYCHE_WALLET_URL = f"{B2C_BASE_URL}/tyche/v1/wallet-summary"
EVA_LATEST_SESSION_URL = f"{B2C_BASE_URL}/eva/v1/sessions/latest"
EVA_CHARGE_HISTORY_URL = f"{B2C_BASE_URL}/eva/v2/order/receipts"
EVA_UNPAID_URL = f"{B2C_BASE_URL}/eva/v1/order/unpaid"
NOTIFICATIONS_URL = f"{B2C_BASE_URL}/notifications/v1/notifications"

# Monetary conversion (Bill.DOLLARS_TO_CENTS = 100.0f)
CENTS_PER_DOLLAR = 100.0
EVA_INTEGER_CENTS_MIN = 100

# Known static route segments for PII-redacted URL logging
KNOWN_ROUTE_SEGMENTS = frozenset(
    {
        "oauth",
        "token",
        "mfa",
        "authenticators",
        "challenge",
        "yggdrasil",
        "graphql",
        "jarvis",
        "v1",
        "v2",
        "v3",
        "v4",
        "v5",
        "me",
        "ami",
        "charts",
        "ppms",
        "balance",
        "smrd-uportal",
        "greengoals",
        "targets",
        "frosty",
        "consumption",
        "fcu_status",
        "njord",
        "payables",
        "history",
        "premises",
        "skalbox",
        "b2c",
        "smrd",
        "retrieveMeterReadDetails",
        "retrieveMeterReadingHistory",
        "account",
        "retrieveBillPreferences",
        "priceplan",
        "plans",
        "price",
        "1up",
        "authenticated",
        "tyche",
        "wallet-summary",
        "eva",
        "sessions",
        "latest",
        "order",
        "receipts",
        "unpaid",
        "notifications",
    }
)

# Exact GraphQL queries from YggdrasilConstants, QueryConstants & TengahQueryConstants
QUERY_YCS_PREMISES_DETAILS_V3 = """
query {
  premiseDetails {
    premises {
      accountNo
      active
      signedUp
      contestableType
      sharedResource {
        owner
        ownerIamId
        ownerName
        ownerDisplayName
        ownerEmail
      }
      accounts {
        accountNo
        bpNo
        bpName
        bpType
        emailAddress
        accountStatus
        accountType
        retailerIdentifier
        retailerName
        premiseNo
        premiseAddressFull
        premiseAddress {
          blockNo
          floor
          unitNo
          street
          postalCode
        }
        system
        ebsAccountNo
        msslAccountNo
        ppms
        moveInDate
        moveOutDate
        activeServices
      }
    }
    linkStatus
  }
}
""".strip()

QUERY_YCS_GET_METER_TYPE = """
query ($accountNo: String!) {
  meterTypeV1 (accountNo: $accountNo) {
    ami
    interval
  }
}
""".strip()

QUERY_YCS_GET_PREMISE_TYPE = """
query ($accountNo: String!) {
  premiseTypeForOwnerV1(accountNo: $accountNo)
}
""".strip()

QUERY_GREENUP_ACCOUNT_V2 = """
query {
  account {
    node {
      id
      totalPoints
      projectedLevelStatus
      advocadoId
      tier {
        node {
          id
          level
          name
          description
          levelStartingPoint
          pointsToLevelUp
          resetPoints
          safePoints
        }
      }
    }
  }
}
""".strip()

TENGAH_PAIRED_FCUS_QUERY = (
    "query($utilityAccountNumber: String!) { "
    "getPairedFCUs(utilityAccountNumber: $utilityAccountNumber)"
    "{ displayName thingName } }"
)
