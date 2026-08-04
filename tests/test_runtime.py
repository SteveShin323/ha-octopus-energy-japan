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
    normalize_historical_selection,
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
                properties=(
                    OejpProperty(
                        id="old-property",
                        supply_points=(
                            OejpSupplyPoint(
                                id="OLD-ACCOUNT-SPIN",
                                account_number="OLD-ACCOUNT",
                                lifecycle=ResourceLifecycle.HISTORICAL,
                            ),
                        ),
                    ),
                ),
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
    assert len(devices) == 5
    provider_ids = (
        "ACTIVE-ACCOUNT",
        "OLD-ACCOUNT",
        "ACTIVE-SPIN",
        "OLD-SPIN",
        "OLD-ACCOUNT-SPIN",
    )
    # Names and identifiers stay ordinal, so a screenshot or a pasted automation never
    # carries a contract number.
    for device in devices:
        for provider_id in provider_ids:
            assert provider_id not in (device.name or "")
            assert all(provider_id not in value for _, value in device.identifiers)
    # The serial number is the deliberate exception: a customer with more than one
    # account or supply point has to be able to tell which device is which, and Home
    # Assistant's device page is where a serial belongs.
    assert {device.serial_number for device in devices} >= {"ACTIVE-ACCOUNT", "ACTIVE-SPIN"}

    active_account = registry.async_get_device(
        identifiers={(DOMAIN, stable_account_identity(SECRET, "ACTIVE-ACCOUNT"))}
    )
    historical_account = registry.async_get_device(
        identifiers={(DOMAIN, stable_account_identity(SECRET, "OLD-ACCOUNT"))}
    )
    active_supply_point = registry.async_get_device(
        identifiers={
            (DOMAIN, stable_supply_point_identity(SECRET, "ACTIVE-ACCOUNT", "ACTIVE-SPIN"))
        }
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
    account_child = registry.async_get_device(
        identifiers={
            (
                DOMAIN,
                stable_supply_point_identity(SECRET, "OLD-ACCOUNT", "OLD-ACCOUNT-SPIN"),
            )
        }
    )
    assert historical_account is not None and historical_account.disabled_by is None
    assert historical_supply_point is not None
    assert historical_supply_point.disabled_by is None
    assert account_child is not None and account_child.disabled_by is None


def test_historical_selection_rejects_stale_children_and_account_owns_children() -> None:
    runtime = _runtime()
    account_id = stable_account_identity(SECRET, "OLD-ACCOUNT")
    account_child_id = stable_supply_point_identity(
        SECRET,
        "OLD-ACCOUNT",
        "OLD-ACCOUNT-SPIN",
    )
    active_account_child_id = stable_supply_point_identity(
        SECRET,
        "ACTIVE-ACCOUNT",
        "OLD-SPIN",
    )

    assert normalize_historical_selection(
        runtime.accounts,
        SECRET,
        (account_child_id, active_account_child_id),
    ) == (active_account_child_id,)
    assert normalize_historical_selection(
        runtime.accounts,
        SECRET,
        (account_id, account_child_id),
    ) == (account_id,)


async def test_missing_devices_are_disabled_and_reappearance_reuses_identity(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = _runtime()
    async_project_discovered_devices(hass, entry, runtime)
    registry = dr.async_get(hass)
    active_identity = stable_supply_point_identity(
        SECRET,
        "ACTIVE-ACCOUNT",
        "ACTIVE-SPIN",
    )
    original = registry.async_get_device(identifiers={(DOMAIN, active_identity)})
    assert original is not None

    runtime.accounts = ()
    async_project_discovered_devices(hass, entry, runtime)
    missing = registry.async_get_device(identifiers={(DOMAIN, active_identity)})
    assert missing is not None and missing.disabled_by is not None
    assert missing.disabled_by.value == "integration"

    runtime.accounts = _runtime().accounts
    async_project_discovered_devices(hass, entry, runtime)
    reappeared = registry.async_get_device(identifiers={(DOMAIN, active_identity)})
    assert reappeared is not None
    assert reappeared.id == original.id
    assert reappeared.disabled_by is None


async def test_active_account_transition_to_selected_history_then_deselection(
    hass: HomeAssistant,
) -> None:
    """Keep registry identity while lifecycle and reconfigure selection change."""
    account_identity = stable_account_identity(SECRET, "ACTIVE-ACCOUNT")
    supply_point_identity = stable_supply_point_identity(
        SECRET,
        "ACTIVE-ACCOUNT",
        "ACTIVE-SPIN",
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = _runtime()
    async_project_discovered_devices(hass, entry, runtime)
    registry = dr.async_get(hass)
    original_account = registry.async_get_device(identifiers={(DOMAIN, account_identity)})
    original_point = registry.async_get_device(identifiers={(DOMAIN, supply_point_identity)})
    assert original_account is not None
    assert original_point is not None

    runtime.accounts = (
        OejpAccount(
            number="ACTIVE-ACCOUNT",
            lifecycle=ResourceLifecycle.HISTORICAL,
            properties=(
                OejpProperty(
                    id="active-property",
                    supply_points=(
                        OejpSupplyPoint(
                            id="ACTIVE-SPIN",
                            account_number="ACTIVE-ACCOUNT",
                            lifecycle=ResourceLifecycle.HISTORICAL,
                        ),
                    ),
                ),
            ),
        ),
    )
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_ENABLED_HISTORICAL_RESOURCES: [account_identity]},
    )
    async_project_discovered_devices(hass, entry, runtime)

    selected_account = registry.async_get_device(identifiers={(DOMAIN, account_identity)})
    selected_point = registry.async_get_device(identifiers={(DOMAIN, supply_point_identity)})
    assert selected_account is not None and selected_account.id == original_account.id
    assert selected_point is not None and selected_point.id == original_point.id
    assert selected_account.disabled_by is None
    assert selected_point.disabled_by is None

    hass.config_entries.async_update_entry(
        entry,
        options={CONF_ENABLED_HISTORICAL_RESOURCES: []},
    )
    async_project_discovered_devices(hass, entry, runtime)

    deselected_account = registry.async_get_device(identifiers={(DOMAIN, account_identity)})
    deselected_point = registry.async_get_device(identifiers={(DOMAIN, supply_point_identity)})
    assert deselected_account is not None
    assert deselected_point is not None
    assert deselected_account.disabled_by is dr.DeviceEntryDisabler.INTEGRATION
    assert deselected_point.disabled_by is dr.DeviceEntryDisabler.INTEGRATION


async def test_lifecycle_projection_preserves_user_disabled_choice(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = _runtime()
    async_project_discovered_devices(hass, entry, runtime)
    registry = dr.async_get(hass)
    identity = stable_supply_point_identity(
        SECRET,
        "ACTIVE-ACCOUNT",
        "ACTIVE-SPIN",
    )
    device = registry.async_get_device(identifiers={(DOMAIN, identity)})
    assert device is not None
    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)

    runtime.accounts = ()
    async_project_discovered_devices(hass, entry, runtime)
    runtime.accounts = _runtime().accounts
    async_project_discovered_devices(hass, entry, runtime)

    preserved = registry.async_get_device(identifiers={(DOMAIN, identity)})
    assert preserved is not None
    assert preserved.disabled_by is dr.DeviceEntryDisabler.USER


async def test_the_device_page_carries_the_identifier_a_bill_shows(
    hass: HomeAssistant,
) -> None:
    """A household with two supply points must be able to tell them apart.

    Entity ids, entity names, and device names are ordinal so they can be screenshotted
    and pasted into a public issue. That leaves nothing to match against a contract, so
    the account number and the supply-point number (供給地点特定番号) go where Home
    Assistant puts a device's own serial: the device page.
    """
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = _runtime()

    async_project_discovered_devices(hass, entry, runtime)

    registry = dr.async_get(hass)
    account = registry.async_get_device(
        identifiers={(DOMAIN, stable_account_identity(runtime.identity_secret, "ACTIVE-ACCOUNT"))}
    )
    supply_point = registry.async_get_device(
        identifiers={
            (
                DOMAIN,
                stable_supply_point_identity(
                    runtime.identity_secret,
                    "ACTIVE-ACCOUNT",
                    "ACTIVE-SPIN",
                ),
            )
        }
    )

    assert account is not None
    assert account.serial_number == "ACTIVE-ACCOUNT"
    assert supply_point is not None
    assert supply_point.serial_number == "ACTIVE-SPIN"
    # The ordinal name is still what appears everywhere else.
    assert supply_point.name is not None
    assert "ACTIVE-SPIN" not in supply_point.name


async def test_a_supply_point_without_a_spin_falls_back_to_its_internal_id(
    hass: HomeAssistant,
) -> None:
    """`spin` is the customer-facing number, but the provider may omit it."""
    from custom_components.octopus_energy_japan.api import (
        OejpAccount,
        OejpProperty,
        OejpSupplyPoint,
        ResourceLifecycle,
    )
    from custom_components.octopus_energy_japan.runtime import OejpRuntimeData

    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    runtime = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=(
            OejpAccount(
                number="A-1",
                lifecycle=ResourceLifecycle.ACTIVE,
                properties=(
                    OejpProperty(
                        id="P-1",
                        supply_points=(
                            OejpSupplyPoint(
                                id="INTERNAL-ONLY",
                                account_number="A-1",
                                lifecycle=ResourceLifecycle.ACTIVE,
                                spin=None,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        capabilities=CapabilitySnapshot(),
        identity_secret=SECRET,
    )

    async_project_discovered_devices(hass, entry, runtime)

    supply_point = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, stable_supply_point_identity(SECRET, "A-1", "INTERNAL-ONLY"))}
    )

    assert supply_point is not None
    assert supply_point.serial_number == "INTERNAL-ONLY"
