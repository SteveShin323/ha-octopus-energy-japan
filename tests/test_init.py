"""Tests for the OEJP config-entry lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.octopus_energy_japan import (
    _async_discover_state,
    async_migrate_entry,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.octopus_energy_japan.adder_baseline import AdderBaselineError
from custom_components.octopus_energy_japan.aggregation import AggregationSnapshot
from custom_components.octopus_energy_japan.api import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpGraphQLError,
    OejpProperty,
    OejpQueryValidationError,
    OejpRateLimitError,
    OejpSupplyPoint,
)
from custom_components.octopus_energy_japan.config_flow import (
    OctopusEnergyJapanConfigFlow,
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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
    # The projector now receives a tariff lookup, because a cost series can only be
    # published from the tariff the commercial coordinator reads on its slower cadence.
    projector_factory.assert_called_once()
    assert projector_factory.call_args.args == (hass, "01" * 32)
    # Calling it is the point. A lookup that is merely callable would also have been
    # captured had it read the wrong coordinator or the wrong attribute, and the closure is
    # what feeds prices to the cost projector.
    tariff_lookup = projector_factory.call_args.kwargs["tariff_lookup"]
    assert callable(tariff_lookup)
    # `data` was None throughout setup, and the closure still resolves afterwards — which
    # is the indirection's whole purpose, since the tariff arrives on a twelve-hour cadence
    # while statistics project every thirty minutes.
    sentinel = object()
    commercial_coordinator.data = Mock()
    commercial_coordinator.data.tariff = Mock(return_value=sentinel)
    assert tariff_lookup("A-1", "SP-1") is sentinel
    commercial_coordinator.data.tariff.assert_called_once_with("A-1", "SP-1")
    # Before the first commercial refresh there is no data, and a cost series must simply
    # not appear rather than raising inside statistics projection.
    commercial_coordinator.data = None
    assert tariff_lookup("A-1", "SP-1") is None
    assert coordinator_factory.call_args.kwargs["statistics_projector"] is statistics_projector
    # A price arriving must itself provoke a statistics pass. The lookup above only answers
    # when something asks, and the only thing that asks is a projection — so without this
    # listener the price waits for the next poll, up to half an hour later.
    listeners = [call.args[0] for call in commercial_coordinator.async_add_listener.call_args_list]
    repriced: list[str] = []

    async def _reprice() -> None:
        repriced.append("repriced")

    coordinator.async_reprice_statistics = _reprice
    for listener in listeners:
        listener()
    await hass.async_block_till_done()
    assert repriced == ["repriced"]
    commercial_factory.assert_called_once()
    project_devices.assert_called_once_with(hass, entry, entry.runtime_data)
    forward.assert_awaited_once_with(entry, ["sensor", "binary_sensor", "button"])
    # Devices come before the first refresh, because the statistics that refresh
    # publishes take their names from the supply-point devices — the Energy dashboard
    # picker shows that name and nothing else. Optional commercial operations stay last
    # so they never delay setup, entity creation, or the first consumption refresh.
    assert events == ["devices", "refresh", "platforms", "background", "commercial"]


async def test_a_broken_adder_baseline_degrades_setup_instead_of_failing_it(
    hass: HomeAssistant,
) -> None:
    """A shipped-file bug must not behave worse than a corrupt per-account archive.

    A corrupt per-account archive is quarantined, not fatal (`tariff_history_store.py`). The
    shipped baseline warm-up should fail the same soft way: setup still succeeds, and every
    `adder_lookup` call afterwards prices from the account's own archive alone, exactly as if
    the baseline had never shipped.
    """
    entry = _entry()
    implementation = AsyncMock()
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
    coordinator = AsyncMock()
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.data = _empty_coordinator_data()
    commercial_coordinator = AsyncMock()
    commercial_coordinator.async_add_listener = Mock(return_value=Mock())
    commercial_coordinator.set_accounts = Mock()
    commercial_coordinator.data = None
    statistics_projector = AsyncMock()

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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
        patch(
            "custom_components.octopus_energy_japan.statistics_runtime.HomeAssistantStatisticsProjector",
            return_value=statistics_projector,
        ) as projector_factory,
        patch("custom_components.octopus_energy_japan.runtime.async_project_discovered_devices"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch(
            "custom_components.octopus_energy_japan.adder_baseline.baseline_generated_at",
            side_effect=AdderBaselineError("the shipped file is broken"),
        ),
    ):
        assert await async_setup_entry(hass, entry)

    adder_lookup = projector_factory.call_args.kwargs["adder_lookup"]
    assert callable(adder_lookup)
    # No account was ever observed either, so this is an empty schedule either way — the
    # point is that reaching it at all didn't re-raise the same read failure a second time.
    schedule = adder_lookup("PRIVATE-ACCOUNT", "PRIVATE-SPIN")
    assert schedule.records == ()


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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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


def _account_with_points(*spins: str) -> OejpAccount:
    return OejpAccount(
        number="PRIVATE-ACCOUNT",
        properties=(
            OejpProperty(
                id="PRIVATE-PROPERTY",
                supply_points=tuple(
                    OejpSupplyPoint(
                        id=f"PRIVATE-POINT-{index}",
                        spin=spin,
                        account_number="PRIVATE-ACCOUNT",
                    )
                    for index, spin in enumerate(spins, start=1)
                ),
            ),
        ),
    )


async def test_a_discovered_supply_start_reaches_the_supply_point() -> None:
    """It anchors the billing period the tariff's steps accumulate over.

    Asked account-scoped because the field is refused through the viewer path the discovery
    document uses, measured on a real account as AUTHORIZATION/KT-CT-4501.
    """
    accounts = (_account_with_points("PRIVATE-SPIN-A"),)
    start = datetime(2026, 6, 17, 15, tzinfo=UTC)

    with (
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=accounts),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={"PRIVATE-SPIN-A": start}),
        ) as supply_starts,
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=CapabilitySnapshot()),
        ),
    ):
        discovered, _capabilities = await _async_discover_state(AsyncMock())

    assert [call.args[1] for call in supply_starts.await_args_list] == ["PRIVATE-ACCOUNT"]
    points = [
        point
        for account in discovered
        for property_ in account.properties
        for point in property_.supply_points
    ]
    assert [point.supply_start_at for point in points] == [start]


async def test_a_refused_supply_start_leaves_the_calendar_month_in_charge() -> None:
    """Consumption does not need it, so a refusal must not stop the entry setting up."""
    accounts = (_account_with_points("PRIVATE-SPIN-A"),)

    with (
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=accounts),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(side_effect=OejpAuthorizationError((GraphQLErrorDetail("safe"),))),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_detect_capabilities",
            AsyncMock(return_value=CapabilitySnapshot()),
        ),
    ):
        discovered, _capabilities = await _async_discover_state(AsyncMock())

    points = [
        point
        for account in discovered
        for property_ in account.properties
        for point in property_.supply_points
    ]
    assert [point.supply_start_at for point in points] == [None]


async def test_a_supply_start_request_still_reauthenticates() -> None:
    """An expired session is not an absent capability, so it must reach Home Assistant."""
    accounts = (_account_with_points("PRIVATE-SPIN-A"),)

    with (
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_resources",
            AsyncMock(return_value=accounts),
        ),
        patch(
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(
                side_effect=OejpAuthenticationError(
                    (GraphQLErrorDetail("safe", error_type="AUTHENTICATION"),)
                )
            ),
        ),
        pytest.raises(OejpAuthenticationError),
    ):
        await _async_discover_state(AsyncMock())


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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor", "button"])
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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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
    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor", "button"])
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

    unload.assert_awaited_once_with(entry, ["sensor", "binary_sensor", "button"])
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
            "custom_components.octopus_energy_japan.api.async_discover_supply_starts",
            AsyncMock(return_value={}),
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


async def test_removing_a_password_entry_makes_no_provider_request(
    hass: HomeAssistant,
) -> None:
    """Nothing can be revoked, so nothing is attempted.

    `invalidateRefreshToken` is rejected for an account user with
    `AUTHORIZATION/KT-CT-1111`. Home Assistant deletes the entry data, so the local
    copy of the credential goes with it and the refresh token expires at the provider.
    """
    entry = _password_entry()
    with patch(
        "custom_components.octopus_energy_japan.password_auth.OejpPasswordAuthSession",
        Mock(side_effect=AssertionError("removal must not build a session")),
    ):
        await async_remove_entry(hass, entry)


async def test_setup_entry_refuses_an_authentication_method_it_does_not_implement(
    hass: HomeAssistant,
) -> None:
    """A downgrade must not be silently treated as OAuth.

    Falling through to the OAuth branch would try the wrong credentials and report a
    confusing failure. Failing on the method itself says what is actually wrong.
    """
    entry = _password_entry(auth_method="something_newer")

    with pytest.raises(ConfigEntryAuthFailed, match="Unsupported"):
        await async_setup_entry(hass, entry)


async def test_setup_entry_treats_a_device_entry_as_an_oauth_entry(
    hass: HomeAssistant,
) -> None:
    """Device-grant tokens come from the same endpoint, so they take the same path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "device",
            "auth_implementation": "test",
            "token": {"access_token": "access", "refresh_token": "refresh", "expires_at": 1e10},
        },
    )
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = OejpOAuthError("invalid")
    with (
        patch(
            "homeassistant.helpers.config_entry_oauth2_flow.async_get_config_entry_implementation",
            AsyncMock(return_value=AsyncMock()),
        ) as implementation,
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

    # Reaching the OAuth session at all is the assertion: a device entry must not be
    # routed to the password branch, which would look for a credential it never had.
    implementation.assert_awaited_once()


async def test_removing_a_device_entry_revokes_like_an_oauth_entry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "device",
            "auth_implementation": "test",
            "token": {"access_token": "access", "refresh_token": "refresh"},
        },
    )
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

    auth.async_revoke.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("reauth", ConfigEntryAuthFailed),
        ("transient", ConfigEntryNotReady),
        ("request", ConfigEntryNotReady),
    ],
)
async def test_setup_entry_maps_each_oauth_token_request_failure(
    hass: HomeAssistant,
    error: str,
    expected: type[Exception],
) -> None:
    """Only an expired or revoked authorization may ask the user to reconnect.

    A transient token-endpoint failure that surfaced as reauthentication would prompt
    every user during a provider outage.
    """
    from aiohttp import RequestInfo
    from homeassistant.exceptions import (
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
        OAuth2TokenRequestTransientError,
    )
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    # These subclass `aiohttp.ClientResponseError`, so they need request info as well
    # as the domain they generate their translated message from.
    request_info = RequestInfo(
        url=URL("https://auth.example.test/token"),
        method="POST",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://auth.example.test/token"),
    )
    failures = {
        "reauth": OAuth2TokenRequestReauthError(request_info=request_info, domain=DOMAIN),
        "transient": OAuth2TokenRequestTransientError(request_info=request_info, domain=DOMAIN),
        "request": OAuth2TokenRequestError(request_info=request_info, domain=DOMAIN),
    }
    entry = _entry()
    auth = AsyncMock()
    auth.async_get_authorization_header.side_effect = failures[error]
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
        pytest.raises(expected),
    ):
        await async_setup_entry(hass, entry)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            OejpAuthenticationError(
                (GraphQLErrorDetail(message="expired", error_code="KT-CT-1120"),)
            ),
            ConfigEntryAuthFailed,
        ),
        (
            OejpRateLimitError(
                (GraphQLErrorDetail(message="rate limited", error_code="KT-CT-1199"),)
            ),
            ConfigEntryNotReady,
        ),
        (OejpQueryValidationError((GraphQLErrorDetail(message="bad"),)), ConfigEntryNotReady),
    ],
)
async def test_setup_entry_maps_each_discovery_failure(
    hass: HomeAssistant,
    error: Exception,
    expected: type[Exception],
) -> None:
    """Discovery can fail after authentication succeeded, and it must not over-report."""
    entry = _entry()
    auth = AsyncMock()
    auth.async_get_authorization_header.return_value = "Bearer access"
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
            AsyncMock(side_effect=error),
        ),
        pytest.raises(expected),
    ):
        await async_setup_entry(hass, entry)


def _seed_storage(hass: HomeAssistant, keys: tuple[str, ...]) -> None:
    """Create real files in the storage directory for the removal scan to find.

    The test harness replaces `Store` with an in-memory double, so the scan has nothing
    to discover unless the files exist. What is asserted below is therefore which keys
    removal *selects*, which is where the logic lives; deleting the file itself is Home
    Assistant's `Store.async_remove`.
    """
    from pathlib import Path

    from homeassistant.helpers.storage import STORAGE_DIR

    directory = Path(hass.config.path(STORAGE_DIR))
    directory.mkdir(parents=True, exist_ok=True)
    for key in keys:
        (directory / key).write_text("{}", encoding="utf-8")


async def _removed_keys(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    removed: list[str] = []

    async def record(self: object) -> None:
        removed.append(self.key)  # type: ignore[attr-defined]

    with patch("homeassistant.helpers.storage.Store.async_remove", record):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    return removed


async def test_removing_an_entry_deletes_the_readings_it_stored(hass: HomeAssistant) -> None:
    """Removal is the user asking for their data to be gone, and the docs say it is.

    Home Assistant deletes the entry's own data and nothing else. The ledger partitions
    hold the account number, the supply-point number, and every stored reading, so they
    have to be deleted here or they survive removal indefinitely.
    """
    entry = _password_entry()
    entry.add_to_hass(hass)
    scope = "supply-point-abc123"
    mine = (
        f"{DOMAIN}.ledger.{entry.entry_id}.{scope}.index",
        f"{DOMAIN}.ledger.{entry.entry_id}.{scope}.2026-07",
        f"{DOMAIN}.ledger.{entry.entry_id}.{scope}.2026-08",
        f"{DOMAIN}.sync.{entry.entry_id}.{scope}",
    )
    someone_elses = f"{DOMAIN}.ledger.other-entry.{scope}.2026-08"
    unrelated = "core.restore_state"
    _seed_storage(hass, (*mine, someone_elses, unrelated, "octopus_energy_japan.identity"))

    removed = await _removed_keys(hass, entry)

    assert set(mine) <= set(removed)
    assert someone_elses not in removed
    assert unrelated not in removed
    # Every month collected is deleted, however many there were.
    assert sum(1 for key in removed if ".ledger." in key) == 3
    # This was the last entry, so the shared secret goes too.
    assert "octopus_energy_japan.identity" in removed


async def test_removing_one_of_two_entries_keeps_the_shared_secret(
    hass: HomeAssistant,
) -> None:
    """The secret makes device and statistic identities stable across entries.

    Deleting it while another entry remains would rename that entry's entities and
    orphan its Energy Dashboard statistics.
    """
    first = _password_entry()
    first.add_to_hass(hass)
    second = _password_entry()
    second.add_to_hass(hass)
    _seed_storage(
        hass,
        (f"{DOMAIN}.sync.{first.entry_id}.point", "octopus_energy_japan.identity"),
    )

    removed = await _removed_keys(hass, first)

    assert f"{DOMAIN}.sync.{first.entry_id}.point" in removed
    assert "octopus_energy_japan.identity" not in removed
    assert second.entry_id in {
        entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)
    }


_SCOPE_DIGEST = "a" * 64
_OTHER_DIGEST = "b" * 64


async def _cleared_statistics(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    listed: tuple[str, ...] = (),
) -> tuple[list[str], int]:
    """Remove `entry` and report which statistic ids were cleared, and how often listed."""
    cleared: list[str] = []
    listings = 0

    def clear(statistic_ids: list[str]) -> None:
        cleared.extend(statistic_ids)

    async def list_ids(_hass: HomeAssistant) -> list[dict[str, str]]:
        nonlocal listings
        listings += 1
        return [
            {"statistic_id": statistic_id, "source": statistic_id.split(":", 1)[0]}
            for statistic_id in listed
        ]

    hass.config.components.add("recorder")
    instance = Mock()
    instance.async_clear_statistics = clear
    with (
        patch("homeassistant.helpers.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            list_ids,
        ),
        patch("homeassistant.helpers.storage.Store.async_remove", AsyncMock()),
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    return cleared, listings


async def test_removing_an_entry_deletes_the_statistics_it_published(
    hass: HomeAssistant,
) -> None:
    """Statistics are not owned by the entry, so Home Assistant leaves them behind.

    Keeping them would not preserve anything usable: the installation secret goes with the
    last entry and every statistic id is an HMAC of it, so a re-install derives new ids and
    the old rows become permanently unreachable clutter in the Energy dashboard picker.
    """
    entry = _password_entry()
    entry.add_to_hass(hass)
    scope = f"supply-point-{_SCOPE_DIGEST}"
    _seed_storage(
        hass,
        (
            f"{DOMAIN}.ledger.{entry.entry_id}.{scope}.index",
            f"{DOMAIN}.ledger.{entry.entry_id}.{scope}.2026-08",
            f"{DOMAIN}.sync.{entry.entry_id}.{scope}",
            f"{DOMAIN}.ledger.other-entry.supply-point-{_OTHER_DIGEST}.2026-08",
        ),
    )
    orphan = f"{DOMAIN}:sp_{'c' * 64}_import_energy"

    cleared, listings = await _cleared_statistics(
        hass,
        entry,
        listed=(orphan, "sensor.something_else"),
    )

    assert f"{DOMAIN}:sp_{_SCOPE_DIGEST}_import_energy" in cleared
    assert f"{DOMAIN}:sp_{_SCOPE_DIGEST}_export_tariff_cost" in cleared
    # This was the last entry, so what an earlier removal orphaned is swept as well.
    assert listings == 1
    assert orphan in cleared
    # Another integration's statistics are never touched.
    assert "sensor.something_else" not in cleared


async def test_removing_one_of_two_entries_keeps_the_others_statistics(
    hass: HomeAssistant,
) -> None:
    """The sweep is only safe when no entry remains.

    Scoping by the store filenames is what keeps a two-account installation from losing the
    surviving entry's energy history.
    """
    first = _password_entry()
    first.add_to_hass(hass)
    second = _password_entry()
    second.add_to_hass(hass)
    _seed_storage(
        hass,
        (
            f"{DOMAIN}.sync.{first.entry_id}.supply-point-{_SCOPE_DIGEST}",
            f"{DOMAIN}.sync.{second.entry_id}.supply-point-{_OTHER_DIGEST}",
        ),
    )

    cleared, listings = await _cleared_statistics(hass, first)

    assert f"{DOMAIN}:sp_{_SCOPE_DIGEST}_import_energy" in cleared
    assert all(_OTHER_DIGEST not in statistic_id for statistic_id in cleared)
    # Listing every statistic under this source would have included the surviving entry's.
    assert listings == 0


async def test_a_recorder_that_cannot_answer_still_deletes_the_readings(
    hass: HomeAssistant,
) -> None:
    """A database that is not answering must not keep the stored readings on disk.

    The readings hold the account number, the supply-point number, and every value, so they
    are deleted first and the statistics are attempted afterwards.
    """
    from sqlalchemy.exc import OperationalError

    entry = _password_entry()
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.sync.{entry.entry_id}.supply-point-{_SCOPE_DIGEST}"
    _seed_storage(hass, (key,))

    async def unavailable(_hass: HomeAssistant) -> list[dict[str, str]]:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    hass.config.components.add("recorder")
    with (
        patch(
            "homeassistant.components.recorder.statistics.async_list_statistic_ids",
            unavailable,
        ),
        patch("homeassistant.helpers.recorder.get_instance", Mock()),
    ):
        removed = await _removed_keys(hass, entry)

    assert key in removed
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_removing_an_entry_that_stored_nothing_clears_no_statistics(
    hass: HomeAssistant,
) -> None:
    """An entry removed before it ever collected a reading has no statistics to delete.

    Its stores carry no supply-point identity, so there is no id to build, and another entry
    remains so the sweep must not run either.
    """
    first = _password_entry()
    first.add_to_hass(hass)
    second = _password_entry()
    second.add_to_hass(hass)
    _seed_storage(hass, (f"{DOMAIN}.sync.{first.entry_id}.pending",))

    def fail(_hass: HomeAssistant) -> object:
        raise AssertionError("The recorder instance must not be requested")

    hass.config.components.add("recorder")
    with (
        patch("homeassistant.helpers.recorder.get_instance", fail),
        patch("homeassistant.helpers.storage.Store.async_remove", AsyncMock()),
    ):
        await hass.config_entries.async_remove(first.entry_id)
        await hass.async_block_till_done()

    assert [entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)] == [
        second.entry_id
    ]


async def test_removal_without_the_recorder_clears_no_statistics(
    hass: HomeAssistant,
) -> None:
    """Nothing was ever published, and asking for the instance would raise."""
    entry = _password_entry()
    entry.add_to_hass(hass)
    _seed_storage(hass, (f"{DOMAIN}.sync.{entry.entry_id}.supply-point-{_SCOPE_DIGEST}",))

    def fail(_hass: HomeAssistant) -> object:
        raise AssertionError("The recorder instance must not be requested")

    with (
        patch("homeassistant.helpers.recorder.get_instance", fail),
        patch("homeassistant.helpers.storage.Store.async_remove", AsyncMock()),
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert not hass.config_entries.async_entries(DOMAIN)


async def test_removal_completes_when_a_store_cannot_be_deleted(
    hass: HomeAssistant,
) -> None:
    """A file the integration cannot delete must not block removing the entry."""
    entry = _password_entry()
    entry.add_to_hass(hass)
    _seed_storage(hass, (f"{DOMAIN}.sync.{entry.entry_id}.point",))

    with patch(
        "homeassistant.helpers.storage.Store.async_remove",
        AsyncMock(side_effect=OSError("read-only")),
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert not hass.config_entries.async_entries(DOMAIN)


async def test_removal_tolerates_a_missing_storage_directory(hass: HomeAssistant) -> None:
    """A fresh installation removed before it ever wrote anything."""
    entry = _password_entry()
    entry.add_to_hass(hass)

    removed = await _removed_keys(hass, entry)

    # Nothing of this entry's was stored, but it was the last entry, so the shared
    # secret is still attempted.
    assert all(entry.entry_id not in key for key in removed)


async def test_a_current_entry_migrates_without_change(hass: HomeAssistant) -> None:
    """Nothing needs migrating yet, and the handler must say so rather than fail.

    Its value is that it exists: Home Assistant refuses to load an entry whose major version
    differs from the flow's when no handler is defined, logging "Migration handler not found".
    Without this function the next increase of `ConfigFlow.VERSION` would break every existing
    entry, and the cause would be the missing handler rather than the schema change.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=OctopusEnergyJapanConfigFlow.VERSION)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True


async def test_an_entry_from_a_newer_version_is_refused(hass: HomeAssistant) -> None:
    """A downgrade must not load an entry this build cannot understand.

    Returning True would accept whatever the newer version stored and then read it with the
    older code, which is how a config entry silently loses fields.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=OctopusEnergyJapanConfigFlow.VERSION + 1)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False


async def test_home_assistant_finds_the_migration_handler() -> None:
    """The handler has to be reachable as `<component>.async_migrate_entry`.

    Home Assistant looks it up with `hasattr` on the component module, so a correct function
    in the wrong place would leave the entry unloadable exactly as if it were absent.
    """
    import custom_components.octopus_energy_japan as component

    assert hasattr(component, "async_migrate_entry")
