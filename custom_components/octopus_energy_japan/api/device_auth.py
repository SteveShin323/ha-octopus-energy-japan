"""RFC 8628 Device Authorization Grant client for OEJP."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession, ContentTypeError


class DeviceAuthorizationError(RuntimeError):
    """Safe base error for Device Authorization Grant."""


class DeviceAuthorizationPendingError(DeviceAuthorizationError):
    """The user has not completed authorization yet."""


class DeviceAuthorizationSlowDownError(DeviceAuthorizationPendingError):
    """The authorization server asked the client to poll more slowly."""


class DeviceAuthorizationDeniedError(DeviceAuthorizationError):
    """The user denied authorization."""


class DeviceAuthorizationExpiredError(DeviceAuthorizationError):
    """The device authorization expired before completion."""


class DeviceAuthorizationTransientError(DeviceAuthorizationError):
    """The authorization server is temporarily unavailable."""


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    """User instructions and polling parameters from the authorization server."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


class OejpDeviceAuthorizationClient:
    """Public-client Device Authorization Grant transport."""

    def __init__(
        self,
        session: ClientSession,
        *,
        device_authorization_url: str,
        token_url: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._device_authorization_url = device_authorization_url
        self._token_url = token_url
        self._sleep = sleep
        self._monotonic = monotonic

    async def async_start(
        self,
        client_id: str,
        scopes: tuple[str, ...],
    ) -> DeviceAuthorization:
        """Start device authorization without using a client secret."""
        payload = await self._post_form(
            self._device_authorization_url,
            {
                "client_id": client_id,
                "scope": " ".join(scopes),
            },
        )
        return DeviceAuthorization(
            device_code=_required_string(payload, "device_code"),
            user_code=_required_string(payload, "user_code"),
            verification_uri=_required_string(payload, "verification_uri"),
            verification_uri_complete=_optional_string(payload.get("verification_uri_complete")),
            expires_in=_positive_int(payload, "expires_in"),
            interval=_positive_int(payload, "interval", default=5),
        )

    async def async_poll_token(
        self,
        client_id: str,
        device_code: str,
    ) -> dict[str, Any]:
        """Poll once for an OAuth token."""
        payload = await self._post_form(
            self._token_url,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
            allow_oauth_error=True,
        )
        token: dict[str, Any] = {
            "access_token": _required_string(payload, "access_token"),
            "expires_in": _positive_int(payload, "expires_in"),
        }
        for key in ("refresh_token", "token_type", "scope", "id_token"):
            if value := _optional_string(payload.get(key)):
                token[key] = value
        return token

    async def async_wait_for_token(
        self,
        client_id: str,
        authorization: DeviceAuthorization,
    ) -> dict[str, Any]:
        """Poll at the provider interval until authorized or expired."""
        deadline = self._monotonic() + authorization.expires_in
        interval = authorization.interval
        while self._monotonic() < deadline:
            await self._sleep(interval)
            if self._monotonic() >= deadline:
                break
            try:
                return await self.async_poll_token(
                    client_id,
                    authorization.device_code,
                )
            except DeviceAuthorizationSlowDownError:
                interval += 5
            except DeviceAuthorizationPendingError:
                continue
        raise DeviceAuthorizationExpiredError("Device authorization expired")

    async def _post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        allow_oauth_error: bool = False,
    ) -> dict[str, Any]:
        try:
            async with self._session.post(url, data=data) as response:
                if response.status == 429 or response.status >= 500:
                    await response.read()
                    raise DeviceAuthorizationTransientError(
                        "Device authorization server is temporarily unavailable"
                    )
                payload = await _decode_payload(response)
        except ClientError as err:
            raise DeviceAuthorizationTransientError("Device authorization request failed") from err

        if response.status >= 400:
            error = payload.get("error")
            if allow_oauth_error and isinstance(error, str):
                _raise_oauth_poll_error(error)
            raise DeviceAuthorizationError("Device authorization request was rejected")
        return payload


async def _decode_payload(response: ClientResponse) -> dict[str, Any]:
    try:
        payload = await response.json(content_type=None)
    except (ContentTypeError, ValueError) as err:
        raise DeviceAuthorizationError("Device authorization response was not valid JSON") from err
    if not isinstance(payload, dict):
        raise DeviceAuthorizationError("Device authorization response was malformed")
    return payload


def _raise_oauth_poll_error(error: str) -> None:
    normalized = error.strip().lower()
    if normalized == "authorization_pending":
        raise DeviceAuthorizationPendingError("Device authorization is pending")
    if normalized == "slow_down":
        raise DeviceAuthorizationSlowDownError("Device authorization polling must slow down")
    if normalized == "access_denied":
        raise DeviceAuthorizationDeniedError("Device authorization was denied")
    if normalized == "expired_token":
        raise DeviceAuthorizationExpiredError("Device authorization expired")
    raise DeviceAuthorizationError("Device authorization token request was rejected")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = _optional_string(payload.get(key))
    if value is None:
        raise DeviceAuthorizationError(f"Device authorization response was missing {key}")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_int(
    payload: dict[str, Any],
    key: str,
    *,
    default: int | None = None,
) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DeviceAuthorizationError(f"Device authorization response contained invalid {key}")
    return value
