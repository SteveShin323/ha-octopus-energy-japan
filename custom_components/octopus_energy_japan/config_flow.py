"""Config flow for Octopus Energy Japan, over each supported login method."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    OejpAuthenticationError,
    OejpError,
    OejpGraphQLClient,
    OejpRateLimitError,
    OejpTransportError,
    async_get_viewer_identity,
    async_obtain_token,
)
from .const import (
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_PASSWORD,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_ENABLED_HISTORICAL_RESOURCES,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from .identity import async_get_identity_secret, stable_login_identity
from .oauth_metadata import OEJP_AUTH_ISSUER, AuthorizationHeaderScheme, OejpOAuthMetadata
from .runtime import (
    OejpRuntimeData,
    normalize_historical_selection,
    selected_historical_resources,
)

_LOGGER = logging.getLogger(__name__)
_TRANSIENT_ERRORS = (OejpRateLimitError, OejpTransportError)

# Home Assistant picks the OAuth redirect URI in
# `config_entry_oauth2_flow.async_get_redirect_uri`: the shared
# `https://my.home-assistant.io/redirect/oauth` when the `my` integration is
# loaded, otherwise this instance's own `/auth/external/callback` URL. Only the
# shared one was submitted to OEJP for registration, because one static URI is
# what lets a single public client serve every installation. Without `my`, the
# flow would send an unregistered redirect URI and the user would land on the
# provider's own error page mid-sign-in, with nothing naming this integration as
# the cause. Fail here instead, while the message can still explain itself.
_MY_HOME_ASSISTANT_DOMAIN = "my"


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
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Let the user pick a login method.

        OAuth is listed first because it is the method OEJP is moving to, and the
        only one that never hands this integration a password. Email and password is
        offered because it is the only method that works before a client ID exists,
        and because OEJP has not yet withdrawn it.
        """
        return self.async_show_menu(
            step_id="user",
            menu_options=[AUTH_METHOD_OAUTH, AUTH_METHOD_PASSWORD],
        )

    async def async_step_oauth(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start browser sign-in, unless the redirect URI would be unregistered."""
        if _MY_HOME_ASSISTANT_DOMAIN not in self.hass.config.components:
            return self.async_abort(reason="my_home_assistant_required")
        return await super().async_step_user(user_input)

    async def async_step_password(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Sign in with the provider's email and password login."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            client = OejpGraphQLClient(async_get_clientsession(self.hass))
            try:
                token = await async_obtain_token(client, email, password)
            except OejpAuthenticationError:
                errors["base"] = "invalid_auth"
            except _TRANSIENT_ERRORS:
                errors["base"] = "cannot_connect"
            except OejpError:
                errors["base"] = "unknown"
            else:
                return await self._async_finish_password_login(email, password, token)

        return self.async_show_form(
            step_id=AUTH_METHOD_PASSWORD,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def _async_finish_password_login(
        self,
        email: str,
        password: str,
        token: Any,
    ) -> ConfigFlowResult:
        """Create or update the one entry this login owns."""
        scheme = AuthorizationHeaderScheme.BEARER.value
        try:
            subject = await async_get_viewer_identity(
                OejpGraphQLClient(async_get_clientsession(self.hass)),
                f"{scheme} {token.access_token}",
            )
        except OejpAuthenticationError:
            return self.async_abort(reason="oauth_unauthorized")
        except _TRANSIENT_ERRORS:
            return self.async_abort(reason="cannot_connect")
        except OejpError:
            return self.async_abort(reason="oauth_identity_unavailable")

        refresh_expires_at = token.refresh_expires_at
        entry_data = {
            CONF_AUTH_METHOD: AUTH_METHOD_PASSWORD,
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_ACCESS_TOKEN: token.access_token,
            CONF_REFRESH_TOKEN: token.refresh_token,
            CONF_REFRESH_EXPIRES_AT: (
                refresh_expires_at.isoformat() if refresh_expires_at is not None else None
            ),
            "oauth_issuer": OEJP_AUTH_ISSUER,
        }
        return await self._async_create_or_update(subject, entry_data)

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

        entry_data = {
            **data,
            CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
            "oauth_issuer": metadata.issuer,
        }
        return await self._async_create_or_update(subject, entry_data)

    async def _async_create_or_update(
        self,
        subject: str,
        entry_data: dict[str, Any],
    ) -> ConfigFlowResult:
        """Own one entry per OEJP login, whichever method authenticated it.

        The identity is scoped to `OEJP_AUTH_ISSUER` rather than to the method, so
        the same `viewer.id` yields the same `unique_id` under every method. That is
        what lets an entry created with email and password be promoted to OAuth in
        place, keeping its stored readings and Energy Dashboard history, instead of
        appearing as a second account.
        """
        secret = await async_get_identity_secret(self.hass)
        await self.async_set_unique_id(stable_login_identity(secret, OEJP_AUTH_ISSUER, subject))

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            entry = self._get_reauth_entry()
            previous_method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_OAUTH)
            if previous_method == entry_data[CONF_AUTH_METHOD]:
                return self.async_update_reload_and_abort(entry, data_updates=entry_data)
            # The method changed, so the old method's keys must not survive. Merging
            # would leave a stored password behind on an entry now using OAuth, and
            # leave a stale OAuth token on an entry now using a password. Replace
            # the data outright, keeping the options and the entry itself.
            return self.async_update_reload_and_abort(entry, data=entry_data)

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
        """Confirm reconnection, then offer the login methods again.

        Reauthentication returns to the method menu rather than straight to the
        method already in use, because that is also how an entry created with email
        and password is promoted to OAuth without losing its stored history.
        """
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select historical resources while active resources remain automatic."""
        entry = self._get_reconfigure_entry()
        runtime = getattr(entry, "runtime_data", None)
        if not isinstance(runtime, OejpRuntimeData):
            return self.async_abort(reason="reconfigure_unavailable")

        options = runtime.historical_resource_options()
        if not options:
            return self.async_abort(reason="no_historical_resources")

        if user_input is not None:
            requested = user_input.get(CONF_ENABLED_HISTORICAL_RESOURCES, [])
            enabled = (
                normalize_historical_selection(
                    runtime.accounts,
                    runtime.identity_secret,
                    (value for value in requested if isinstance(value, str) and value in options),
                )
                if isinstance(requested, list)
                else ()
            )
            return self.async_update_reload_and_abort(
                entry,
                options={
                    **entry.options,
                    CONF_ENABLED_HISTORICAL_RESOURCES: list(enabled),
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
