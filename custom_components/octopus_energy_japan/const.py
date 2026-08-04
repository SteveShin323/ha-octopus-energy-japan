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
