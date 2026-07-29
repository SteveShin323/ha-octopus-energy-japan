"""Home Assistant OAuth sessions for OEJP."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AuthSession
from .oauth_metadata import OejpOAuthMetadata


class OejpOAuthError(RuntimeError):
    """Safe base error for OAuth session operations."""


class OejpOAuthRevocationError(OejpOAuthError):
    """OAuth authorization could not be revoked."""


class OejpPkceAuthSession(AuthSession):
    """Runtime OAuth session for Authorization Code + PKCE."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        implementation: config_entry_oauth2_flow.AbstractOAuth2Implementation,
        metadata: OejpOAuthMetadata,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._implementation = implementation
        self._metadata = metadata
        self._session = config_entry_oauth2_flow.OAuth2Session(
            hass,
            entry,
            implementation,
        )
        self._refresh_lock = asyncio.Lock()
        self._request_access_token: ContextVar[str | None] = ContextVar(
            "oejp_request_access_token",
            default=None,
        )

    async def async_get_authorization_header(self) -> str:
        """Return a valid provider-confirmed Authorization header."""
        await self._session.async_ensure_token_valid()
        access_token = self._required_access_token(self._session.token)
        self._request_access_token.set(access_token)
        scheme = self._metadata.authorization_scheme.value
        return f"{scheme} {access_token}" if scheme else access_token

    async def async_refresh(self) -> None:
        """Force one token refresh while coalescing concurrent rejected requests."""
        rejected_token = self._request_access_token.get()
        async with self._refresh_lock:
            current_token = self._required_access_token(self._session.token)
            if rejected_token is not None and current_token != rejected_token:
                return
            new_token = await self._implementation.async_refresh_token(self._session.token)
            self._hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, "token": new_token},
            )

    async def async_revoke(self) -> None:
        """Revoke the refresh token, or access token when no refresh token exists."""
        if self._metadata.revocation_url is None:
            return
        token = self._session.token
        refresh_token = token.get("refresh_token")
        access_token = self._required_access_token(token)
        token_to_revoke = refresh_token if isinstance(refresh_token, str) else access_token
        token_type_hint = "refresh_token" if isinstance(refresh_token, str) else "access_token"
        client_id = getattr(self._implementation, "client_id", None)
        request_data = {
            "token": token_to_revoke,
            "token_type_hint": token_type_hint,
        }
        if isinstance(client_id, str) and client_id:
            request_data["client_id"] = client_id
        try:
            response = await async_get_clientsession(self._hass).post(
                self._metadata.revocation_url,
                data=request_data,
            )
            async with response:
                await response.read()
                if response.status >= 400:
                    raise OejpOAuthRevocationError("OEJP OAuth revocation was rejected")
        except ClientError as err:
            raise OejpOAuthRevocationError("OEJP OAuth revocation request failed") from err

    @staticmethod
    def _required_access_token(token: dict[str, Any]) -> str:
        access_token = token.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OejpOAuthError("OAuth token did not contain an access token")
        return access_token


class OejpDeviceAuthSession(OejpPkceAuthSession):
    """Runtime session for tokens obtained through Device Authorization Grant."""
