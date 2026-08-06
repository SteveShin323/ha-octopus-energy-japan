"""Tests for the one control this integration offers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from custom_components.octopus_energy_japan.aggregation import AggregationSnapshot
from custom_components.octopus_energy_japan.api import (
    CapabilitySnapshot,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.button import (
    OejpImportHistoryButton,
    async_setup_entry,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import OejpCoordinatorData
from custom_components.octopus_energy_japan.identity import stable_supply_point_identity
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
SECRET = "01" * 32
ACCOUNT_ID = "PRIVATE-ACCOUNT"
SUPPLY_POINT_ID = "PRIVATE-SUPPLY-POINT"
IDENTITY = stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID)


def _accounts() -> tuple[OejpAccount, ...]:
    return (
        OejpAccount(
            number=ACCOUNT_ID,
            lifecycle=ResourceLifecycle.ACTIVE,
            properties=(
                OejpProperty(
                    id="PRIVATE-PROPERTY",
                    supply_points=(
                        OejpSupplyPoint(
                            id=SUPPLY_POINT_ID,
                            account_number=ACCOUNT_ID,
                            lifecycle=ResourceLifecycle.ACTIVE,
                        ),
                    ),
                ),
            ),
        ),
    )


def _coordinator(*, running: bool = True) -> Mock:
    coordinator = Mock()
    coordinator.accounts = _accounts()
    coordinator.data = OejpCoordinatorData(
        accounts=_accounts(),
        capabilities=CapabilitySnapshot(),
        aggregation=AggregationSnapshot((), NOW),
        present_supply_points=frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}),
        enabled_supply_points=frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}),
    )
    coordinator.async_add_listener = Mock(return_value=Mock())
    coordinator.async_start_history_backfill = AsyncMock()
    coordinator.has_running_backfill = Mock(return_value=running)
    coordinator.last_update_success = True
    return coordinator


def _entry(hass: HomeAssistant, coordinator: Mock | None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=_accounts(),
        capabilities=CapabilitySnapshot(),
        identity_secret=SECRET,
        coordinator=coordinator,
    )
    return entry


async def test_one_button_per_supply_point_added_once(hass: HomeAssistant) -> None:
    coordinator = _coordinator()
    add_entities = Mock()

    await async_setup_entry(hass, _entry(hass, coordinator), add_entities)

    (entities,) = add_entities.call_args.args
    assert [type(entity) for entity in entities] == [OejpImportHistoryButton]
    # Named by the installation-local identity, never by the provider's own.
    assert SUPPLY_POINT_ID not in str(entities[0].unique_id)
    assert ACCOUNT_ID not in str(entities[0].unique_id)

    add_entities.reset_mock()
    listener = coordinator.async_add_listener.call_args.args[0]
    listener()
    add_entities.assert_not_called()


async def test_the_platform_refuses_to_set_up_without_a_coordinator(
    hass: HomeAssistant,
) -> None:
    with pytest.raises(PlatformNotReady):
        await async_setup_entry(hass, _entry(hass, None), Mock())


async def test_pressing_starts_the_walk(hass: HomeAssistant) -> None:
    coordinator = _coordinator()
    button = OejpImportHistoryButton(coordinator, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID)

    await button.async_press()

    coordinator.async_start_history_backfill.assert_awaited_once_with(IDENTITY)


async def test_pressing_says_so_when_nothing_can_be_walked(hass: HomeAssistant) -> None:
    """The legacy path returns the most recent 31 days however far back it is asked.

    Starting anyway would collect a month and report a complete history, so the walk refuses —
    and refusing silently would leave the user pressing a button that appears to do nothing.
    """
    coordinator = _coordinator(running=False)
    button = OejpImportHistoryButton(coordinator, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID)

    with pytest.raises(ServiceValidationError) as raised:
        await button.async_press()

    assert raised.value.translation_key == "backfill_unsupported"
    assert raised.value.translation_domain == DOMAIN


async def test_an_entity_without_a_unique_id_is_a_defect_not_a_silent_skip(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A button with no unique id could never be disabled or renamed by the user."""
    monkeypatch.setattr(OejpImportHistoryButton, "unique_id", property(lambda _self: None))

    with pytest.raises(RuntimeError, match="unique ID"):
        await async_setup_entry(hass, _entry(hass, _coordinator()), Mock())
