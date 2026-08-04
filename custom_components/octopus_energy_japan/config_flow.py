"""Config flow for Octopus Energy Japan, over each supported login method."""

from __future__ import annotations

import logging
from asyncio import Task
from collections.abc import Mapping
from time import time
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DeviceAuthorization,
    DeviceAuthorizationDeniedError,
    DeviceAuthorizationError,
    DeviceAuthorizationExpiredError,
    DeviceAuthorizationTransientError,
    OejpAuthenticationError,
    OejpDeviceAuthorizationClient,
    OejpError,
    OejpGraphQLClient,
    OejpRateLimitError,
    OejpTransportError,
    async_get_viewer_identity,
    async_obtain_token,
)
from .const import (
    AUTH_METHOD_DEVICE,
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

# Home Assistant stores the chosen implementation under this key in entry data. It has
# no exported constant, and the authorization-code path writes it through Home
# Assistant's own flow, so the device path has to write the same literal.
_CONF_AUTH_IMPLEMENTATION = "auth_implementation"


class OctopusEnergyJapanConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle OAuth2 authentication for Octopus Energy Japan."""

    DOMAIN = DOMAIN
    VERSION = 2

    # Device-flow state, held only for the duration of one flow.
    _device_auth_domain: str | None = None
    _device_metadata: OejpOAuthMetadata | None = None
    _device_client: OejpDeviceAuthorizationClient | None = None
    _device_authorization: DeviceAuthorization | None = None
    _device_task: Task[dict[str, Any]] | None = None

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
            menu_options=[AUTH_METHOD_OAUTH, AUTH_METHOD_DEVICE, AUTH_METHOD_PASSWORD],
        )

    async def async_step_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Sign in with the Device Authorization Grant.

        This needs the same client ID as browser sign-in but no redirect URI, so it
        does not depend on My Home Assistant. Once a client ID exists it is the
        simplest of the three for a headless or remote Home Assistant.
        """
        implementations = await config_entry_oauth2_flow.async_get_implementations(
            self.hass,
            self.DOMAIN,
        )
        # A device-grant client is identified by its client ID alone, so only an
        # implementation exposing one can be used. `AbstractOAuth2Implementation` does
        # not declare `client_id`; the local PKCE implementation registered by
        # `application_credentials.py` does, along with the provider metadata.
        candidates: dict[str, tuple[str, OejpOAuthMetadata, str]] = {}
        for domain, implementation in sorted(implementations.items()):
            client_id = getattr(implementation, "client_id", None)
            metadata = getattr(implementation, "metadata", None)
            if isinstance(client_id, str) and client_id and isinstance(metadata, OejpOAuthMetadata):
                candidates[domain] = (client_id, metadata, implementation.name)
        if not candidates:
            return self.async_abort(reason="missing_credentials")

        if len(candidates) > 1:
            if user_input is None or _CONF_AUTH_IMPLEMENTATION not in user_input:
                return self.async_show_form(
                    step_id=AUTH_METHOD_DEVICE,
                    data_schema=vol.Schema(
                        {
                            vol.Required(_CONF_AUTH_IMPLEMENTATION): vol.In(
                                {domain: name for domain, (_, _, name) in candidates.items()}
                            )
                        }
                    ),
                )
            auth_domain = user_input[_CONF_AUTH_IMPLEMENTATION]
        else:
            auth_domain = next(iter(candidates))

        client_id, device_metadata, _name = candidates[auth_domain]
        if device_metadata.device_authorization_url is None:
            return self.async_abort(reason="device_grant_unavailable")

        self._device_auth_domain = auth_domain
        self._device_metadata = device_metadata
        self._device_client = OejpDeviceAuthorizationClient(
            async_get_clientsession(self.hass),
            device_authorization_url=device_metadata.device_authorization_url,
            token_url=device_metadata.token_url,
        )
        try:
            self._device_authorization = await self._device_client.async_start(
                client_id,
                device_metadata.scopes,
            )
        except DeviceAuthorizationTransientError:
            return self.async_abort(reason="cannot_connect")
        except DeviceAuthorizationError:
            return self.async_abort(reason="device_grant_unavailable")

        self._device_task = self.hass.async_create_task(
            self._device_client.async_wait_for_token(
                client_id,
                self._device_authorization,
            )
        )
        return await self.async_step_device_authorize()

    async def async_step_device_authorize(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the user code while polling the provider for a token."""
        authorization = self._device_authorization
        task = self._device_task
        if authorization is None or task is None:
            return self.async_abort(reason="device_grant_unavailable")
        if not task.done():
            return self.async_show_progress(
                step_id="device_authorize",
                progress_action="wait_for_device",
                progress_task=task,
                description_placeholders={
                    "user_code": authorization.user_code,
                    "url": authorization.verification_uri_complete
                    or authorization.verification_uri,
                },
            )
        return self.async_show_progress_done(next_step_id="device_finish")

    async def async_step_device_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Turn the polled token into an entry, or explain why there is none."""
        task = self._device_task
        metadata = self._device_metadata
        auth_domain = self._device_auth_domain
        self._device_task = None
        if task is None or metadata is None or auth_domain is None:
            return self.async_abort(reason="device_grant_unavailable")
        try:
            token = task.result()
        except DeviceAuthorizationDeniedError:
            return self.async_abort(reason="user_rejected_authorize")
        except DeviceAuthorizationExpiredError:
            return self.async_abort(reason="device_code_expired")
        except DeviceAuthorizationError:
            return self.async_abort(reason="cannot_connect")

        # The device grant returns `expires_in`, while Home Assistant's OAuth session
        # refreshes on `expires_at`. The authorization-code path gets this from Home
        # Assistant's own token request; this path has to supply it.
        token = {**token, "expires_at": time() + float(token["expires_in"])}

        scheme = metadata.authorization_scheme.value
        access_token = token["access_token"]
        try:
            subject = await async_get_viewer_identity(
                OejpGraphQLClient(async_get_clientsession(self.hass)),
                f"{scheme} {access_token}" if scheme else access_token,
            )
        except OejpAuthenticationError:
            return self.async_abort(reason="oauth_unauthorized")
        except _TRANSIENT_ERRORS:
            return self.async_abort(reason="cannot_connect")
        except OejpError:
            return self.async_abort(reason="oauth_identity_unavailable")

        return await self._async_create_or_update(
            subject,
            {
                CONF_AUTH_METHOD: AUTH_METHOD_DEVICE,
                _CONF_AUTH_IMPLEMENTATION: auth_domain,
                "token": token,
                "oauth_issuer": metadata.issuer,
            },
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
