"""Tests for OEJP reading availability binary sensors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, Mock

from custom_components.octopus_energy_japan.aggregation import (
    AggregatedInterval,
    AggregationSnapshot,
    PeriodAggregate,
    SupplyPointAggregation,
)
from custom_components.octopus_energy_japan.api import (
    CapabilitySnapshot,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
    ReadingDirection,
)
from custom_components.octopus_energy_japan.binary_sensor import (
    OejpDataAvailableBinarySensor,
    async_setup_entry,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import (
    OejpCoordinatorData,
    OejpDataUpdateCoordinator,
)
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
SECRET = "02" * 32
ACCOUNT_ID = "PRIVATE-ACCOUNT"
SUPPLY_POINT_ID = "PRIVATE-SUPPLY-POINT"


def _coordinator(*, with_reading: bool = True) -> OejpDataUpdateCoordinator:
    point = OejpSupplyPoint(
        id=SUPPLY_POINT_ID,
        account_number=ACCOUNT_ID,
        direction=ReadingDirection.IMPORT,
    )
    account = OejpAccount(
        number=ACCOUNT_ID,
        properties=(OejpProperty(id="PRIVATE-PROPERTY", supply_points=(point,)),),
    )
    latest = (
        AggregatedInterval(
            account_id=ACCOUNT_ID,
            supply_point_id=SUPPLY_POINT_ID,
            direction=ReadingDirection.IMPORT,
            start_at=NOW - timedelta(hours=1),
            end_at=NOW - timedelta(minutes=30),
            energy_kwh=Decimal("0.25"),
            official_cost=None,
            contributing_records=1,
            correction_count=0,
        )
        if with_reading
        else None
    )
    aggregate = SupplyPointAggregation(
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.IMPORT,
        latest=latest,
        today=PeriodAggregate(),
        yesterday=PeriodAggregate(),
        this_week=PeriodAggregate(),
        this_month=PeriodAggregate(),
        last_month=PeriodAggregate(),
        latest_reading_end=latest.end_at if latest is not None else None,
        data_delay=timedelta(minutes=30) if latest is not None else None,
    )
    coordinator = Mock()
    coordinator.accounts = (account,)
    coordinator.capabilities = CapabilitySnapshot()
    coordinator.last_update_success = True
    coordinator.data = OejpCoordinatorData(
        accounts=(account,),
        capabilities=CapabilitySnapshot(),
        aggregation=AggregationSnapshot((aggregate,), NOW),
        present_supply_points=frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}),
    )
    coordinator.async_add_listener.return_value = Mock()
    return cast("OejpDataUpdateCoordinator", coordinator)


def test_data_available_reflects_completed_ledger_intervals() -> None:
    available = OejpDataAvailableBinarySensor(
        _coordinator(),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
    )
    empty = OejpDataAvailableBinarySensor(
        _coordinator(with_reading=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
    )

    assert available.is_on
    assert not empty.is_on
    assert available.translation_key == "import_data_available"
    assert ACCOUNT_ID not in available.unique_id
    assert SUPPLY_POINT_ID not in available.unique_id


async def test_binary_sensor_platform_adds_each_entity_once(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=coordinator.accounts,
        capabilities=coordinator.capabilities,
        identity_secret=SECRET,
        coordinator=coordinator,
    )
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert len(add_entities.call_args.args[0]) == 1
    listener = cast("Mock", coordinator.async_add_listener).call_args.args[0]
    listener()
    assert add_entities.call_count == 1
