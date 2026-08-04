"""Tests for the OEJP config-entry lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.octopus_energy_japan import (
    _async_discover_state,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.octopus_energy_japan.aggregation import AggregationSnapshot
from custom_components.octopus_energy_japan.api import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthorizationError,
    OejpGraphQLError,
    OejpProperty,
    OejpQueryValidationError,
    OejpRateLimitError,
    OejpSupplyPoint,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import OejpCoordinatorData
from custom_components.octopus_energy_japan.oauth import (
    OejpOAuthError,
    OejpOAuthRevocationError,
)
from custom_components.octopus_energy_japan.oauth_metadata import (
    AuthorizationHeaderScheme,
    OAuthMetadataUnavailableError,
    OejpOAuthMetadata,
)
from custom_components.octopus_energy_japan.password_auth import (
    OejpPasswordCredentialRejected,
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


def _empty_coordinator_data() -> OejpCoordinatorData:
    """Minimal real coordinator data so issue evaluation runs as it does live."""
    return OejpCoordinatorData(
        accounts=(),
        capabilities=CapabilitySnapshot(),
        aggregation=AggregationSnapshot((), datetime(2026, 8, 4, tzinfo=UTC)),
        present_supply_points=frozenset(),
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
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.data = _empty_coordinator_data()
    commercial_coordinator = AsyncMock()
    commercial_coordinator.async_add_listener = Mock(return_value=Mock())
    commercial_coordinator.set_accounts = Mock()
    commercial_coordinator.data = None
    statistics_projector = AsyncMock()
    coordinator.async_config_entry_first_refresh.side_effect = lambda: events.append("refresh")
    coordinator.async_start_background_sync.side_effect = lambda: events.append("background")
    commercial_coordinator.async_request_refresh.side_effect = lambda: events.append("commercial")
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
            "custom_components.octopus_energy_japan.commercial_coordinator.OejpCommercialCoordinator",
            return_value=commercial_coordinator,
        ) as commercial_factory,
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
    assert entry.runtime_data.commercial_coordinator is commercial_coordinator
    auth.async_get_authorization_header.assert_awaited_once_with()
    coordinator.async_config_entry_first_refresh.assert_awaited_once_with()
    commercial_coordinator.async_request_refresh.assert_awaited_once_with()
    commercial_coordinator.async_refresh.assert_not_awaited()
    coordinator.async_start_background_sync.assert_awaited_once_with()
    projector_factory.assert_called_once_with(hass, "01" * 32)
    assert coordinator_factory.call_args.kwargs["statistics_projector"] is statistics_projector
    commercial_factory.assert_called_once()
    project_devices.assert_called_once_with(hass, entry, entry.runtime_data)
    forward.assert_awaited_once_with(entry, ["sensor", "binary_sensor"])
    # Optional commercial operations are armed last so they never delay setup,
    # entity creation, or the first consumption refresh.
    assert events == ["refresh", "devices", "platforms", "background", "commercial"]


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


@pytest.mark.parametrize(
    ("error", "expected_reason"),
    [
        (
            OejpAuthorizationError((GraphQLErrorDetail("safe", error_type="AUTHORIZATION"),)),
            "generic_device_discovery_forbidden",
        ),
        (
            OejpQueryValidationError((GraphQLErrorDetail("safe", error_code="KT-CT-1113"),)),
            "generic_device_schema_mismatch",
        ),
        (
            OejpGraphQLError((GraphQLErrorDetail("safe", error_code="KT-CT-7899"),)),
            "generic_device_discovery_unavailable",
        ),
    ],
)
async def test_generic_device_refusal_degrades_capability_instead_of_failing_setup(
    error: Exception,
    expected_reason: str,
) -> None:
    """A supply point without generic devices must still set up. KT-CT-7899."""
    capabilities = CapabilitySnapshot().replace(
        (Capability.DEVICES, Capability.REGISTERS),
        CapabilityAvailability.SUPPORTED,
        "introspected",
    )
    accounts = (
        OejpAccount(
            number="PRIVATE-ACCOUNT",
            properties=(
                OejpProperty(
                    id="PRIVATE-PROPERTY",
                    supply_points=(
                        OejpSupplyPoint(
                            id="PRIVATE-POINT",
                            spin="PRIVATE-SPIN",
                            account_number="PRIVATE-ACCOUNT",
                        ),
                    ),
                ),
            ),
        ),
    )

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
            AsyncMock(side_effect=error),
        ),
    ):
        discovered, observed = await _async_discover_state(AsyncMock())

    assert discovered == accounts
    for capability in (Capability.DEVICES, Capability.REGISTERS):
        assert observed.availability(capability) in {
            CapabilityAvailability.UNSUPPORTED,
            CapabilityAvailability.FORBIDDEN,
        }
        status = next(s for s in observed.statuses if s.capability is capability)
        assert status.reason == expected_reason


async def test_generic_device_rate_limit_lets_setup_retry() -> None:
    """A temporary refusal must not be recorded as an absent capability."""
    capabilities = CapabilitySnapshot().replace(
        (Capability.DEVICES, Capability.REGISTERS),
        CapabilityAvailability.SUPPORTED,
        "introspected",
    )
    accounts = (
        OejpAccount(
            number="PRIVATE-ACCOUNT",
            properties=(
                OejpProperty(
                    id="PRIVATE-PROPERTY",
                    supply_points=(
                        OejpSupplyPoint(
                            id="PRIVATE-POINT",
                            spin="PRIVATE-SPIN",
                            account_number="PRIVATE-ACCOUNT",
                        ),
                    ),
                ),
            ),
        ),
    )

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
            AsyncMock(
                side_effect=OejpRateLimitError(
                    (GraphQLErrorDetail("safe", error_code="KT-CT-1199"),)
                )
            ),
        ),
        pytest.raises(OejpRateLimitError),
    ):
        await _async_discover_state(AsyncMock())


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
    coordinator.async_add_listener = Mock(return_value=Mock())
    commercial_coordinator = AsyncMock()
    commercial_coordinator.async_add_listener = Mock(return_value=Mock())
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
        patch(
            "custom_components.octopus_energy_japan.commercial_coordinator.OejpCommercialCoordinator",
            return_value=commercial_coordinator,
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
    commercial_coordinator.async_shutdown.assert_awaited_once_with()
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
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.data = _empty_coordinator_data()
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


async def test_unload_also_stops_the_optional_commercial_coordinator(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    coordinator = AsyncMock()
    commercial_coordinator = AsyncMock()
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(),
        capabilities=CapabilitySnapshot(),
        identity_secret="01" * 32,
        coordinator=coordinator,
        commercial_coordinator=commercial_coordinator,
    )

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, entry)

    coordinator.async_shutdown_runtime.assert_awaited_once_with()
    commercial_coordinator.async_shutdown.assert_awaited_once_with()
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


def _password_entry(**overrides: object) -> MockConfigEntry:
    data = {
        "auth_method": "password",
        "email": "person@example.test",
        "password": "correct horse",
        "access_token": "legacy-access",
        "refresh_token": "legacy-refresh",
        "refresh_expires_at": "2026-08-11T00:00:00+00:00",
    }
    data.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=data)


async def test_setup_entry_uses_the_password_session_without_any_oauth_implementation(
    hass: HomeAssistant,
) -> None:
    """A password entry has no application credential, so none may be required."""
    entry = _password_entry()
    session = AsyncMock()
    session.async_get_authorization_header.return_value = "Bearer legacy-access"
    coordinator = AsyncMock()
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.data = _empty_coordinator_data()
    commercial_coordinator = AsyncMock()
    commercial_coordinator.async_add_listener = Mock(return_value=Mock())
    commercial_coordinator.set_accounts = Mock()
    commercial_coordinator.data = None
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(side_effect=AssertionError("the password method must not need one")),
        ),
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
            return_value=session,
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
            "custom_components.octopus_energy_japan.coordinator.OejpDataUpdateCoordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.octopus_energy_japan.commercial_coordinator.OejpCommercialCoordinator",
            return_value=commercial_coordinator,
        ),
        patch(
            "custom_components.octopus_energy_japan.statistics_runtime.HomeAssistantStatisticsProjector",
            return_value=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ),
        patch(
            "custom_components.octopus_energy_japan.runtime.async_project_discovered_devices",
            Mock(),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True

    runtime = entry.runtime_data
    assert isinstance(runtime, OejpRuntimeData)
    assert runtime.auth is session


@pytest.mark.parametrize(
    "overrides",
    [{"password": None}, {"email": None}],
    ids=["password missing", "email missing"],
)
async def test_setup_entry_asks_for_reauth_when_the_credential_is_not_stored(
    hass: HomeAssistant,
    overrides: dict[str, object],
) -> None:
    entry = _password_entry(**overrides)
    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_entry_retries_when_metadata_is_unavailable_for_a_password_entry(
    hass: HomeAssistant,
) -> None:
    entry = _password_entry()
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            side_effect=OAuthMetadataUnavailableError("awaiting confirmation"),
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_asks_for_reauth_when_the_stored_credential_is_rejected(
    hass: HomeAssistant,
) -> None:
    """A rejected password cannot be recovered by retrying, so it must not be."""
    entry = _password_entry()
    session = AsyncMock()
    session.async_get_authorization_header.side_effect = OejpPasswordCredentialRejected("rejected")
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
            return_value=session,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_retries_a_password_sign_in_that_failed_transiently(
    hass: HomeAssistant,
) -> None:
    entry = _password_entry()
    session = AsyncMock()
    session.async_get_authorization_header.side_effect = OejpRateLimitError(
        (GraphQLErrorDetail(message="rate limited", error_code="KT-CT-1199"),)
    )
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
            return_value=session,
        ),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_removing_a_password_entry_invalidates_its_refresh_token(
    hass: HomeAssistant,
) -> None:
    entry = _password_entry()
    session = AsyncMock()
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
            return_value=session,
        ),
    ):
        await async_remove_entry(hass, entry)

    session.async_revoke.assert_awaited_once()


async def test_removing_a_password_entry_without_a_credential_revokes_nothing(
    hass: HomeAssistant,
) -> None:
    entry = _password_entry(password=None)
    with patch(
        "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
        Mock(side_effect=AssertionError("nothing to revoke with")),
    ):
        await async_remove_entry(hass, entry)


async def test_removing_a_password_entry_tolerates_a_failed_invalidation(
    hass: HomeAssistant,
) -> None:
    """Removal must complete even if the provider refuses, or the entry is stuck."""
    entry = _password_entry()
    session = AsyncMock()
    session.async_revoke.side_effect = OejpQueryValidationError(
        (GraphQLErrorDetail(message="refused", error_code="KT-CT-1113"),)
    )
    with (
        patch(
            "custom_components.octopus_energy_japan.oauth_metadata.require_oauth_metadata",
            return_value=METADATA,
        ),
        patch(
            "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
            return_value=session,
        ),
    ):
        await async_remove_entry(hass, entry)
