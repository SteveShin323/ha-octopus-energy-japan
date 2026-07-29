"""Tests for discovered runtime state and HA device projection."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.octopus_energy_japan.api import (
    CapabilitySnapshot,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.const import (
    CONF_ENABLED_HISTORICAL_RESOURCES,
    DOMAIN,
)
from custom_components.octopus_energy_japan.identity import (
    stable_account_identity,
    stable_supply_point_identity,
)
from custom_components.octopus_energy_japan.runtime import (
    OejpRuntimeData,
    async_project_discovered_devices,
    selected_historical_resources,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

SECRET = "01" * 32


def _runtime() -> OejpRuntimeData:
    return OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(
            OejpAccount(
                number="ACTIVE-ACCOUNT",
                lifecycle=ResourceLifecycle.ACTIVE,
                properties=(
                    OejpProperty(
                        id="active-property",
                        supply_points=(
                            OejpSupplyPoint(
                                id="ACTIVE-SPIN",
                                account_number="ACTIVE-ACCOUNT",
                                lifecycle=ResourceLifecycle.ACTIVE,
                            ),
                            OejpSupplyPoint(
                                id="OLD-SPIN",
                                account_number="ACTIVE-ACCOUNT",
                                lifecycle=ResourceLifecycle.HISTORICAL,
                            ),
                        ),
                    ),
                ),
            ),
            OejpAccount(
                number="OLD-ACCOUNT",
                lifecycle=ResourceLifecycle.HISTORICAL,
            ),
        ),
        capabilities=CapabilitySnapshot(),
        identity_secret=SECRET,
    )


def test_historical_resource_options_are_safe_and_deterministic() -> None:
    options = _runtime().historical_resource_options()

    assert list(options.values()) == [
        "Historical supply point 1",
        "Historical account 1",
    ]
    assert all("OLD-" not in value for value in options)
    assert stable_supply_point_identity(SECRET, "ACTIVE-ACCOUNT", "OLD-SPIN") in options
    assert stable_account_identity(SECRET, "OLD-ACCOUNT") in options


def test_selected_historical_resources_rejects_invalid_option_shapes() -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ENABLED_HISTORICAL_RESOURCES: "not-a-list"},
    )
    assert selected_historical_resources(entry) == frozenset()

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_ENABLED_HISTORICAL_RESOURCES: ["safe-id", "", 7]},
    )
    assert selected_historical_resources(entry) == {"safe-id"}


async def test_device_projection_hides_provider_ids_and_disables_history(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = _runtime()

    async_project_discovered_devices(hass, entry, runtime)

    registry = dr.async_get(hass)
    devices = list(registry.devices.values())
    assert len(devices) == 4
    assert all(
        provider_id not in repr(device)
        for provider_id in ("ACTIVE-ACCOUNT", "OLD-ACCOUNT", "ACTIVE-SPIN", "OLD-SPIN")
        for device in devices
    )

    active_account = registry.async_get_device(
        identifiers={(DOMAIN, stable_account_identity(SECRET, "ACTIVE-ACCOUNT"))}
    )
    historical_account = registry.async_get_device(
        identifiers={(DOMAIN, stable_account_identity(SECRET, "OLD-ACCOUNT"))}
    )
    active_supply_point = registry.async_get_device(
        identifiers={(DOMAIN, stable_supply_point_identity(SECRET, "ACTIVE-ACCOUNT", "ACTIVE-SPIN"))}
    )
    historical_supply_point = registry.async_get_device(
        identifiers={(DOMAIN, stable_supply_point_identity(SECRET, "ACTIVE-ACCOUNT", "OLD-SPIN"))}
    )

    assert active_account is not None and active_account.disabled_by is None
    assert active_supply_point is not None and active_supply_point.disabled_by is None
    assert historical_account is not None
    assert historical_account.disabled_by is not None
    assert historical_account.disabled_by.value == "integration"
    assert historical_supply_point is not None
    assert historical_supply_point.disabled_by is not None
    assert historical_supply_point.disabled_by.value == "integration"


async def test_selected_historical_resources_are_enabled(hass: HomeAssistant) -> None:
    historical_account_id = stable_account_identity(SECRET, "OLD-ACCOUNT")
    historical_supply_point_id = stable_supply_point_identity(
        SECRET,
        "ACTIVE-ACCOUNT",
        "OLD-SPIN",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_ENABLED_HISTORICAL_RESOURCES: [
                historical_account_id,
                historical_supply_point_id,
            ]
        },
    )
    entry.add_to_hass(hass)

    async_project_discovered_devices(hass, entry, _runtime())

    registry = dr.async_get(hass)
    historical_account = registry.async_get_device(identifiers={(DOMAIN, historical_account_id)})
    historical_supply_point = registry.async_get_device(
        identifiers={(DOMAIN, historical_supply_point_id)}
    )
    assert historical_account is not None and historical_account.disabled_by is None
    assert historical_supply_point is not None
    assert historical_supply_point.disabled_by is None
