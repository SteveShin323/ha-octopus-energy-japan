"""Tests for the OEJP config-entry lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan import (
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.oauth import (
    OejpOAuthError,
    OejpOAuthRevocationError,
)
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OejpOAuthMetadata,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

METADATA = OejpOAuthMetadata(
    issuer="https://auth.example.test",
    authorize_url="https://auth.example.test/authorize",
    token_url="https://auth.example.test/token",
    scopes=("openid",),
    authorization_scheme=AuthorizationHeaderScheme.BEARER,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "test",
            "token": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 9999999999,
            },
        },
    )


async def test_setup_entry_creates_auth_runtime_and_forwards_platforms(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    implementation = AsyncMock()
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
    with (
        patch(
            "custom_components.octopus_energy_japan.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            AsyncMock(return_value=implementation),
        ),
        patch(
            "custom_components.octopus_energy_japan.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.OejpPkceAuthSession",
            return_value=auth,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data is auth
    auth.async_get_authorization_header.assert_awaited_once_with()
    forward.assert_awaited_once_with(entry, ["sensor"])


async def test_setup_entry_retries_when_oauth_implementation_is_unavailable(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    with (
        patch(
            "custom_components.octopus_energy_japan.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            AsyncMock(
                side_effect=config_entry_oauth2_flow.ImplementationUnavailableError("offline")
            ),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_requests_reauth_for_invalid_token(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = OejpOAuthError("invalid")
    with (
        patch(
            "custom_components.octopus_energy_japan.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.OejpPkceAuthSession",
            return_value=auth,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


async def test_unload_entry_unloads_platforms_and_clears_runtime(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.runtime_data = AsyncMock()
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ) as unload:
        assert await async_unload_entry(hass, entry)

    unload.assert_awaited_once_with(entry, ["sensor"])
    assert entry.runtime_data is None


async def test_remove_entry_revokes_authorization_best_effort(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    with (
        patch(
            "custom_components.octopus_energy_japan.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.OejpPkceAuthSession",
            return_value=auth,
        ),
    ):
        await async_remove_entry(hass, entry)

    auth.async_revoke.assert_awaited_once_with()


async def test_remove_entry_does_not_block_on_revocation_failure(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    auth.async_revoke.side_effect = OejpOAuthRevocationError("failed")
    with (
        patch(
            "custom_components.octopus_energy_japan.config_entry_oauth2_flow."
            "async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.OejpPkceAuthSession",
            return_value=auth,
        ),
    ):
        await async_remove_entry(hass, entry)
