"""Config flow for Octopus Energy Japan."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    OejpAccount,
    OejpAuthenticationError,
    OejpError,
    OejpGraphQLClient,
    OejpRateLimitError,
    OejpTransportError,
    async_discover_accounts,
    async_obtain_token,
)
from .const import CONF_ACCOUNT_NUMBER, DOMAIN
from .identity import async_get_identity_secret, stable_account_identity

_TRANSIENT_ERRORS = (OejpRateLimitError, OejpTransportError)


class NoAccountsError(OejpError):
    """Raised when valid OEJP credentials expose no accounts."""


class OctopusEnergyJapanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Octopus Energy Japan."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize account discovery state."""
        super().__init__()
        self._accounts: dict[str, OejpAccount] = {}
        self._credentials: dict[str, str] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            normalized_input = {
                CONF_EMAIL: user_input[CONF_EMAIL].strip().lower(),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            try:
                client = OejpGraphQLClient(async_get_clientsession(self.hass))
                token = await async_obtain_token(
                    client,
                    normalized_input[CONF_EMAIL],
                    normalized_input[CONF_PASSWORD],
                )
                accounts = await async_discover_accounts(client, token.access_token)
                if not accounts:
                    raise NoAccountsError
            except OejpAuthenticationError:
                errors["base"] = "invalid_auth"
            except _TRANSIENT_ERRORS:
                errors["base"] = "cannot_connect"
            except NoAccountsError:
                errors["base"] = "no_accounts"
            except OejpError:
                errors["base"] = "unknown"
            else:
                self._credentials = normalized_input
                self._accounts = {account.number: account for account in accounts}
                if len(accounts) == 1:
                    return await self._async_create_account_entry(accounts[0])
                return await self.async_step_account()

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_account(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Select one account when the credentials expose multiple accounts."""
        if user_input is not None:
            account_number = user_input[CONF_ACCOUNT_NUMBER]
            account = self._accounts.get(account_number)
            if account is not None:
                return await self._async_create_account_entry(account)

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_NUMBER): vol.In(
                    {number: number for number in sorted(self._accounts)}
                )
            }
        )
        return self.async_show_form(
            step_id="account",
            data_schema=schema,
        )

    async def _async_create_account_entry(self, account: OejpAccount) -> FlowResult:
        """Create an entry with a private identity derived from the selected account."""
        secret = await async_get_identity_secret(self.hass)
        unique_id = stable_account_identity(secret, account.number)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._credentials[CONF_EMAIL],
            data={
                **self._credentials,
                CONF_ACCOUNT_NUMBER: account.number,
            },
        )
