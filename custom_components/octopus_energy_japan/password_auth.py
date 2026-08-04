"""Runtime session for the provider's email and password login.

This is the only authentication method that works without an OAuth client ID, and
OEJP is withdrawing it: `email` and `password` are no longer among the introspected
fields of `ObtainJSONWebTokenInput`, yet the provider still honours them. A field
that is hidden but honoured can stop being honoured without notice, so every failure
that could mean "this login no longer exists" is treated as terminal rather than
retried.

Token lifetimes, measured against a real account on 2026-08-04:

- the access token lives one hour;
- the refresh token lives seven days;
- renewing does **not** rotate the refresh token and does **not** extend its expiry.

So renewal buys at most seven days from the original sign-in, and after that only
the credential itself can produce a new token. That is why the credential is stored,
and it is the whole reason this method differs from the OAuth ones in privacy terms.

A rejected credential and an expired access token both arrive as
`OejpAuthenticationError`, because OEJP reports a wrong password as
`VALIDATION/KT-CT-1138`. They must not be confused: one is recoverable by renewing,
the other is not recoverable at all.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import (
    AuthSession,
    OejpAuthenticationError,
    OejpGraphQLClient,
    OejpToken,
    async_obtain_token,
    async_renew_token,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
)


class OejpPasswordAuthError(RuntimeError):
    """Safe base error for the password session."""


class OejpPasswordCredentialRejected(OejpPasswordAuthError):
    """The stored credential was rejected, so no automatic recovery is possible."""


class OejpPasswordAuthSession(AuthSession):
    """Authenticate with a stored email and password, renewing while it can."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OejpGraphQLClient,
        *,
        email: str,
        password: str,
        scheme: str,
        now: Any = None,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._email = email
        self._password = password
        self._scheme = scheme
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._request_access_token: ContextVar[str | None] = ContextVar(
            "oejp_password_request_access_token",
            default=None,
        )

    async def async_get_authorization_header(self) -> str:
        """Return an Authorization header, signing in first if nothing is stored."""
        access_token = self._stored_access_token()
        if access_token is None:
            async with self._lock:
                # Another request may have signed in while this one waited.
                access_token = self._stored_access_token()
                if access_token is None:
                    access_token = await self._async_sign_in()
        self._request_access_token.set(access_token)
        return f"{self._scheme} {access_token}" if self._scheme else access_token

    async def async_refresh(self) -> None:
        """Renew from the refresh token, falling back to a full sign-in."""
        rejected = self._request_access_token.get()
        async with self._lock:
            current = self._stored_access_token()
            if rejected is not None and current is not None and current != rejected:
                # A concurrent request already replaced the token this caller used.
                return
            refresh_token = self._usable_refresh_token()
            if refresh_token is not None:
                try:
                    await self._async_store(await async_renew_token(self._client, refresh_token))
                except OejpAuthenticationError:
                    # The refresh token is spent, revoked, or past its seven days.
                    # Only the credential can recover from here.
                    await self._async_sign_in()
                return
            await self._async_sign_in()

    async def async_revoke(self) -> None:
        """Do nothing, because the provider does not allow it for this method.

        `invalidateRefreshToken` exists in the schema, and calling it as the signed-in
        account user was rejected with `AUTHORIZATION/KT-CT-1111` on 2026-08-04. The
        provider's own documentation lists `KT-CT-1111` and `KT-CT-1130` Unauthorized
        for that mutation, so it is reserved for callers this method cannot be.

        Removal therefore deletes the local copy of the token and the credential, and
        the refresh token expires on the provider's side within seven days of the
        sign-in that issued it. `PRIVACY.md` states this rather than implying that
        removal revokes anything. An OAuth entry does revoke, at the OAuth revocation
        endpoint, which is a different mechanism entirely.
        """
        return

    async def _async_sign_in(self) -> str:
        try:
            token = await async_obtain_token(self._client, self._email, self._password)
        except OejpAuthenticationError as err:
            # Either the credential changed, or OEJP stopped honouring the hidden
            # email/password fields. Neither is fixed by trying again, and both
            # require the user to act, so this must reach reauthentication rather
            # than a retry loop.
            raise OejpPasswordCredentialRejected(
                "OEJP rejected the stored email and password"
            ) from err
        await self._async_store(token)
        return token.access_token

    async def _async_store(self, token: OejpToken) -> None:
        refresh_expires_at = token.refresh_expires_at
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_ACCESS_TOKEN: token.access_token,
                CONF_REFRESH_TOKEN: token.refresh_token,
                CONF_REFRESH_EXPIRES_AT: (
                    refresh_expires_at.isoformat() if refresh_expires_at is not None else None
                ),
            },
        )

    def _stored_access_token(self) -> str | None:
        value = self._entry.data.get(CONF_ACCESS_TOKEN)
        return value if isinstance(value, str) and value else None

    def _usable_refresh_token(self) -> str | None:
        """Return the refresh token unless it is known to have expired.

        The expiry is stored so an attempt that cannot succeed is not made at all.
        A missing or unparseable expiry is treated as usable, because the provider
        rejecting it is a better answer than this method guessing.
        """
        value = self._entry.data.get(CONF_REFRESH_TOKEN)
        if not isinstance(value, str) or not value:
            return None
        raw_expiry = self._entry.data.get(CONF_REFRESH_EXPIRES_AT)
        if isinstance(raw_expiry, str):
            try:
                expires_at = datetime.fromisoformat(raw_expiry)
            except ValueError:
                return value
            if expires_at.tzinfo is None:
                return value
            if expires_at <= self._now():
                return None
        return value
