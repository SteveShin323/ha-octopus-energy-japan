"""Tests for the OEJP config-entry lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan import (
    _async_discover_state,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.octopus_energy_japan.api import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
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
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
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
    events: list[str] = []
    implementation = AsyncMock()
    auth = AsyncMock()
    coordinator = AsyncMock()
    statistics_projector = AsyncMock()
    coordinator.async_config_entry_first_refresh.side_effect = lambda: events.append("refresh")
    coordinator.async_start_background_sync.side_effect = lambda: events.append("background")
    auth.async_get_authorization_header.return_value = "Bearer access"
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=implementation),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
            return_value=auth,
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=CapabilitySnapshot()),
        ),
        patch(
            "custom_components.octopus_energy_japan.identity.async_get_identity_secret",
            AsyncMock(return_value="01" * 32),
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.OejpDataUpdateCoordinator",
            return_value=coordinator,
        ) as coordinator_factory,
        patch(
            "custom_components.octopus_energy_japan.statistics_runtime.HomeAssistantStatisticsProjector",
            return_value=statistics_projector,
        ) as projector_factory,
        patch(
            "custom_components.octopus_energy_japan.runtime.async_project_discovered_devices",
            side_effect=lambda *_args: events.append("devices"),
        ) as project_devices,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=lambda *_args: events.append("platforms")),
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry)

    assert entry.runtime_data.auth is auth
    assert entry.runtime_data.accounts == ()
    assert entry.runtime_data.coordinator is coordinator
    auth.async_get_authorization_header.assert_awaited_once_with()
    coordinator.async_config_entry_first_refresh.assert_awaited_once_with()
    coordinator.async_start_background_sync.assert_awaited_once_with()
    projector_factory.assert_called_once_with(hass, "01" * 32)
    assert coordinator_factory.call_args.kwargs["statistics_projector"] is statistics_projector
    project_devices.assert_called_once_with(hass, entry, entry.runtime_data)
    forward.assert_awaited_once_with(entry, ["sensor", "binary_sensor"])
    assert events == ["refresh", "devices", "platforms", "background"]


async def test_discovery_queries_generic_topology_sequentially() -> None:
    accounts = (
        OejpAccount(
            number="PRIVATE-ACCOUNT",
            properties=(
                OejpProperty(
                    id="PRIVATE-PROPERTY",
                    supply_points=(
                        OejpSupplyPoint(
                            id="PRIVATE-POINT-A",
                            spin="PRIVATE-SPIN-A",
                            account_number="PRIVATE-ACCOUNT",
                        ),
                        OejpSupplyPoint(
                            id="PRIVATE-POINT-B",
                            spin="PRIVATE-SPIN-B",
                            account_number="PRIVATE-ACCOUNT",
                        ),
                    ),
                ),
            ),
        ),
    )
    capabilities = CapabilitySnapshot(
        (
            CapabilityStatus(
                Capability.DEVICES,
                CapabilityAvailability.SUPPORTED,
            ),
        )
    )
    active = 0
    maximum_active = 0

    async def discover_devices(_client: object, _identifier: str) -> tuple[()]:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return ()

    with (
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=accounts),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=capabilities),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_generic_devices",
            AsyncMock(side_effect=discover_devices),
        ) as topology,
    ):
        discovered, observed_capabilities = await _async_discover_state(AsyncMock())

    assert discovered == accounts
    assert observed_capabilities is capabilities
    assert maximum_active == 1
    assert [call.args[1] for call in topology.await_args_list] == [
        "PRIVATE-SPIN-A",
        "PRIVATE-SPIN-B",
    ]


async def test_setup_entry_retries_when_oauth_implementation_is_unavailable(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
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
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
            return_value=auth,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_failure_cleans_partially_allocated_runtime_and_platforms(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
    coordinator = AsyncMock()
    coordinator.async_config_entry_first_refresh.side_effect = ConfigEntryNotReady("retry")
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
            return_value=auth,
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=CapabilitySnapshot()),
        ),
        patch(
            "custom_components.octopus_energy_japan.identity.async_get_identity_secret",
            AsyncMock(return_value="01" * 32),
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.OejpDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(side_effect=RuntimeError("partial unload failed")),
        ) as unload,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ) as forward,
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)

    coordinator.async_shutdown_runtime.assert_awaited_once_with()
    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor"])
    forward.assert_not_awaited()
    assert entry.runtime_data is None


async def test_platform_forward_failure_is_cleaned_without_masking_error(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
    coordinator = AsyncMock()
    forward_error = RuntimeError("platform failed")
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
            return_value=auth,
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=()),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=CapabilitySnapshot()),
        ),
        patch(
            "custom_components.octopus_energy_japan.identity.async_get_identity_secret",
            AsyncMock(return_value="01" * 32),
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.OejpDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.octopus_energy_japan.runtime.async_project_discovered_devices"
        ) as project_devices,
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=True),
        ) as unload,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=forward_error),
        ),
        pytest.raises(RuntimeError, match="platform failed"),
    ):
        await async_setup_entry(hass, entry)

    project_devices.assert_called_once()
    coordinator.async_shutdown_runtime.assert_awaited_once_with()
    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor"])
    assert entry.runtime_data is None


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

    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor"])
    assert entry.runtime_data is None


async def test_unload_quiesces_worker_before_platforms_and_flushes_after(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    events: list[str] = []
    coordinator = AsyncMock()
    coordinator.async_prepare_shutdown.side_effect = lambda: events.append("prepare")
    coordinator.async_shutdown_runtime.side_effect = lambda: events.append("flush")
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(),
        capabilities=CapabilitySnapshot(),
        identity_secret="01" * 32,
        coordinator=coordinator,
    )

    async def unload(*_args: object) -> bool:
        events.append("platforms")
        return True

    with patch.object(hass.config_entries, "async_unload_platforms", side_effect=unload):
        assert await async_unload_entry(hass, entry)

    assert events == ["prepare", "platforms", "flush"]
    assert entry.runtime_data is None


async def test_unload_failure_restarts_runtime_once_and_retains_runtime_data(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    coordinator = AsyncMock()
    runtime = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(),
        capabilities=CapabilitySnapshot(),
        identity_secret="01" * 32,
        coordinator=coordinator,
    )
    entry.runtime_data = runtime
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    coordinator.async_prepare_shutdown.assert_awaited_once_with()
    coordinator.async_resume_runtime.assert_awaited_once_with()
    coordinator.async_shutdown_runtime.assert_not_awaited()
    assert entry.runtime_data is runtime


async def test_remove_entry_revokes_authorization_best_effort(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    auth = AsyncMock()
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
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
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth.OejpPkceAuthSession",
            return_value=auth,
        ),
    ):
        await async_remove_entry(hass, entry)
