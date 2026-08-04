"""Constants for the Octopus Energy Japan integration."""

DOMAIN = "octopus_energy_japan"
API_URL = "https://api.oejp-kraken.energy/v1/graphql/"
# OEJP reports monetary amounts in the smallest JPY unit, which is one yen, so
# provider values are surfaced without a scaling conversion.
CURRENCY_JPY = "JPY"
DEFAULT_SCAN_INTERVAL_MINUTES = 30
CONF_ACCOUNT_NUMBER = "account_number"
CONF_ENABLED_HISTORICAL_RESOURCES = "enabled_historical_resources"
