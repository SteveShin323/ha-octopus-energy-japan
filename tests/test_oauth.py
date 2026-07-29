"""Tests for Home Assistant OEJP OAuth sessions."""

from __future__ import annotations

import asyncio
from typing import Any, Self, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientError, ClientSession
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.oauth import (
    OejpOAuthError,
    OejpOAuthRevocationError,
    OejpPkceAuthSession,
)
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OejpOAuthMetadata,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

METADATA = OejpOAuthMetadata(
    issuer="https://auth.example.test",
    authorize_url="https://auth.example.test/authorize",
    token_url="https://auth.example.test/token",
    scopes=("openid",),
    authorization_scheme=AuthorizationHeaderScheme.BEARER,
    revocation_url="https://auth.example.test/revoke",
)


class FakeOAuth2Session:
    """Minimal token session backed by config-entry data."""

    def __init__(self, entry: MockConfigEntry) -> None:
        self._entry = entry
        self.ensure_count = 0

    @property
    def token(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._entry.data["token"])

    async def async_ensure_token_valid(self) -> None:
        self.ensure_count += 1


class FakeResponse:
    """Async response used for revocation calls."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.read_called = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def read(self) -> bytes:
        self.read_called = True
        return b""


def _entry(
    *,
    access_token: object = "access",
    refresh_token: object = "refresh",
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "test",
            "token": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": 9999999999,
            },
        },
    )


def _auth(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    implementation: AsyncMock,
    metadata: OejpOAuthMetadata = METADATA,
) -> tuple[OejpPkceAuthSession, FakeOAuth2Session]:
    fake_session = FakeOAuth2Session(entry)
    with patch.object(
        config_entry_oauth2_flow,
        "OAuth2Session",
        return_value=fake_session,
    ):
        auth = OejpPkceAuthSession(hass, entry, implementation, metadata)
    return auth, fake_session


async def test_authorization_header_uses_confirmed_scheme(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth, session = _auth(hass, entry, AsyncMock())

    assert await auth.async_get_authorization_header() == "Bearer access"
    assert session.ensure_count == 1


async def test_raw_authorization_header_is_supported_only_by_metadata(
    hass: HomeAssistant,
) -> None:
    metadata = OejpOAuthMetadata(
        issuer=METADATA.issuer,
        authorize_url=METADATA.authorize_url,
        token_url=METADATA.token_url,
        scopes=(),
        authorization_scheme=AuthorizationHeaderScheme.RAW,
    )
    auth, _ = _auth(hass, _entry(), AsyncMock(), metadata)

    assert await auth.async_get_authorization_header() == "access"


async def test_missing_access_token_is_rejected_without_leaking_token(
    hass: HomeAssistant,
) -> None:
    auth, _ = _auth(hass, _entry(access_token=None), AsyncMock())

    with pytest.raises(OejpOAuthError, match="did not contain"):
        await auth.async_get_authorization_header()


async def test_refresh_rotation_updates_entry_and_coalesces_concurrent_requests(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    implementation = AsyncMock()
    implementation.async_refresh_token.return_value = {
        "access_token": "rotated-access",
        "refresh_token": "rotated-refresh",
        "expires_at": 9999999999,
    }
    auth, _ = _auth(hass, entry, implementation)
    await auth.async_get_authorization_header()

    await asyncio.gather(auth.async_refresh(), auth.async_refresh())

    implementation.async_refresh_token.assert_awaited_once()
    assert entry.data["token"]["access_token"] == "rotated-access"
    assert entry.data["token"]["refresh_token"] == "rotated-refresh"


async def test_revoke_prefers_refresh_token_and_includes_public_client_id(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    implementation = AsyncMock()
    implementation.client_id = "public-client"
    auth, _ = _auth(hass, entry, implementation)
    response = FakeResponse()
    session = Mock(spec=ClientSession)
    session.post.return_value = response

    with patch(
        "custom_components.octopus_energy_japan.oauth.async_get_clientsession",
        return_value=session,
    ):
        await auth.async_revoke()

    session.post.assert_called_once_with(
        METADATA.revocation_url,
        data={
            "token": "refresh",
            "token_type_hint": "refresh_token",
            "client_id": "public-client",
        },
    )
    assert response.read_called


async def test_revoke_is_noop_when_provider_does_not_publish_endpoint(
    hass: HomeAssistant,
) -> None:
    metadata = OejpOAuthMetadata(
        issuer=METADATA.issuer,
        authorize_url=METADATA.authorize_url,
        token_url=METADATA.token_url,
        scopes=(),
        authorization_scheme=AuthorizationHeaderScheme.BEARER,
    )
    auth, _ = _auth(hass, _entry(), AsyncMock(), metadata)
    with patch(
        "custom_components.octopus_energy_japan.oauth.async_get_clientsession"
    ) as get_session:
        await auth.async_revoke()
    get_session.assert_not_called()


async def test_revoke_rejects_http_failure_with_safe_error(
    hass: HomeAssistant,
) -> None:
    implementation = AsyncMock()
    implementation.client_id = "public-client"
    auth, _ = _auth(hass, _entry(), implementation)
    session = Mock(spec=ClientSession)
    session.post.return_value = FakeResponse(status=400)

    with (
        patch(
            "custom_components.octopus_energy_japan.oauth.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(OejpOAuthRevocationError, match="rejected"),
    ):
        await auth.async_revoke()


async def test_revoke_uses_access_token_without_optional_client_id(
    hass: HomeAssistant,
) -> None:
    auth, _ = _auth(hass, _entry(refresh_token=None), AsyncMock())
    response = FakeResponse()
    session = Mock(spec=ClientSession)
    session.post.return_value = response

    with patch(
        "custom_components.octopus_energy_japan.oauth.async_get_clientsession",
        return_value=session,
    ):
        await auth.async_revoke()

    session.post.assert_called_once_with(
        METADATA.revocation_url,
        data={
            "token": "access",
            "token_type_hint": "access_token",
        },
    )


async def test_revoke_wraps_network_failure_with_safe_error(
    hass: HomeAssistant,
) -> None:
    auth, _ = _auth(hass, _entry(), AsyncMock())
    session = Mock(spec=ClientSession)
    session.post.side_effect = ClientError("provider unavailable")

    with (
        patch(
            "custom_components.octopus_energy_japan.oauth.async_get_clientsession",
            return_value=session,
        ),
        pytest.raises(OejpOAuthRevocationError, match="request failed"),
    ):
        await auth.async_revoke()
