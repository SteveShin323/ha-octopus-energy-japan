"""Constants for the Octopus Energy Japan integration."""

DOMAIN = "octopus_energy_japan"
API_URL = "https://api.oejp-kraken.energy/v1/graphql/"
# Provider monetary fields are surfaced without a scaling conversion. Confirmed
# on 2026-08-04 by reconciling provider cost against a real invoice: OEJP
# denominates these fields in whole yen. See `docs/CONTRACT_AND_BILLING.md`.
CURRENCY_JPY = "JPY"
DEFAULT_SCAN_INTERVAL_MINUTES = 30
CONF_ACCOUNT_NUMBER = "account_number"
CONF_ENABLED_HISTORICAL_RESOURCES = "enabled_historical_resources"

# Which authentication method an entry was created with. Absent on entries created
# before more than one method existed, which were all OAuth, so `AUTH_METHOD_OAUTH`
# is the default when the key is missing.
CONF_AUTH_METHOD = "auth_method"
AUTH_METHOD_OAUTH = "oauth"
AUTH_METHOD_DEVICE = "device"
AUTH_METHOD_PASSWORD = "password"
AUTH_METHODS = (AUTH_METHOD_OAUTH, AUTH_METHOD_DEVICE, AUTH_METHOD_PASSWORD)
# Device authorization yields ordinary OAuth tokens from the same token endpoint, so
# its entries are set up and refreshed exactly like authorization-code entries.
OAUTH_AUTH_METHODS = (AUTH_METHOD_OAUTH, AUTH_METHOD_DEVICE)

# Password-method entry data. The credential is stored because the provider's
# refresh token lasts seven days and renewing does not extend it, so nothing else
# can produce a token afterwards. See `docs/adr/0008-password-authentication.md`.
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_REFRESH_EXPIRES_AT = "refresh_expires_at"
