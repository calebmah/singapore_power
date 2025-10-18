"""Constants for the pysingaporepower library."""

# Base URL for the Singapore Power API
API_BASE_URL = "https://api.singaporepower.com.sg/v1" # URL for GraphQL requests
# URL for consumption data
CONSUMPTION_URL = "https://b2c.api.spdigital.sg/jarvis/v3/ami/charts"
# URL for yearly summary data
YEARLY_SUMMARY_URL = "https://b2c.api.spdigital.sg/jarvis/v4/charts/{premise_id}"
# URL for OAuth token requests
OAUTH_TOKEN_URL = "https://identity.spdigital.sg/oauth/token"
MFA_AUTHENTICATORS_URL = "https://identity.spdigital.sg/mfa/authenticators"
MFA_CHALLENGE_URL = "https://identity.spdigital.sg/mfa/challenge"

# Static values from the mobile app
CLIENT_ID = "z1tl6I1V6HI201ule9tmSALb97hw8Biu"
SCOPE = "openid email profile offline_access enroll read:authenticators user_metadata me me:uportal me:eva me:rbac"
GRANT_TYPE_PASSWORD = "http://auth0.com/oauth/grant-type/password-realm"
REALM = "Username-Password-Authentication"
AUDIENCE = "https://profile.up.spdigital.sg/"
GRANT_TYPE_MFA_OOB = "http://auth0.com/oauth/grant-type/mfa-oob"