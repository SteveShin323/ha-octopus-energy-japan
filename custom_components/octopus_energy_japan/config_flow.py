"""OAuth config flow for Octopus Energy Japan."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    OejpAuthenticationError,
    OejpError,
    OejpGraphQLClient,
    OejpRateLimitError,
    OejpTransportError,
    async_get_viewer_identity,
)
from .const import CONF_ENABLED_HISTORICAL_RESOURCES, DOMAIN
from .identity import async_get_identity_secret, stable_login_identity
from .oauth_metadata import OejpOAuthMetadata
from .runtime import OejpRuntimeData, selected_historical_resources

_LOGGER = logging.getLogger(__name__)
_TRANSIENT_ERRORS = (OejpRateLimitError, OejpTransportError)


class OctopusEnergyJapanConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle OAuth2 authentication for Octopus Energy Japan."""

    DOMAIN = DOMAIN
    VERSION = 2

    @property
    @override
    def logger(self) -> logging.Logger:
        """Return the flow logger."""
        return _LOGGER

    @override
    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Validate the OAuth principal and create or update one login-scoped entry."""
        token = data.get("token")
        metadata = getattr(self.flow_impl, "metadata", None)
        if not isinstance(token, dict) or not isinstance(metadata, OejpOAuthMetadata):
            return self.async_abort(reason="oauth_metadata_unavailable")

        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            return self.async_abort(reason="oauth_identity_unavailable")

        scheme = metadata.authorization_scheme.value
        authorization_header = f"{scheme} {access_token}" if scheme else access_token
        client = OejpGraphQLClient(async_get_clientsession(self.hass))
        try:
            subject = await async_get_viewer_identity(client, authorization_header)
        except OejpAuthenticationError:
            return self.async_abort(reason="oauth_unauthorized")
        except _TRANSIENT_ERRORS:
            return self.async_abort(reason="cannot_connect")
        except OejpError:
            return self.async_abort(reason="oauth_identity_unavailable")

        secret = await async_get_identity_secret(self.hass)
        unique_id = stable_login_identity(secret, metadata.issuer, subject)
        await self.async_set_unique_id(unique_id)

        entry_data = {**data, "oauth_issuer": metadata.issuer}
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=entry_data,
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Octopus Energy Japan",
            data=entry_data,
        )

    async def async_step_reauth(
        self,
        _entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start OAuth reauthentication for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm that the user wants to reconnect the OEJP account."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select historical resources while active resources remain automatic."""
        entry = self._get_reconfigure_entry()
        runtime = entry.runtime_data
        if not isinstance(runtime, OejpRuntimeData):
            return self.async_abort(reason="reconfigure_unavailable")

        options = runtime.historical_resource_options()
        if not options:
            return self.async_abort(reason="no_historical_resources")

        if user_input is not None:
            requested = user_input.get(CONF_ENABLED_HISTORICAL_RESOURCES, [])
            enabled = (
                sorted(value for value in requested if isinstance(value, str) and value in options)
                if isinstance(requested, list)
                else []
            )
            return self.async_update_reload_and_abort(
                entry,
                options={
                    **entry.options,
                    CONF_ENABLED_HISTORICAL_RESOURCES: enabled,
                },
            )

        selected = selected_historical_resources(entry)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLED_HISTORICAL_RESOURCES,
                        default=sorted(selected & options.keys()),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=value, label=label)
                                for value, label in options.items()
                            ],
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )
