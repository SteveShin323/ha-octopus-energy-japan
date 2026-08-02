"""Tests for privacy-preserving OEJP sensor projections."""

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
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import (
    DirectionSyncStatus,
    OejpCoordinatorData,
    OejpDataUpdateCoordinator,
)
from custom_components.octopus_energy_japan.identity import (
    stable_account_identity,
    stable_supply_point_identity,
)
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
from custom_components.octopus_energy_japan.sensor import (
    ENERGY_DESCRIPTIONS,
    OejpConsumptionSensor,
    OejpSupplyPointStatusSensor,
    async_setup_entry,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
SECRET = "01" * 32
ACCOUNT_ID = "PRIVATE-ACCOUNT"
SUPPLY_POINT_ID = "PRIVATE-SUPPLY-POINT"


def _account(
    *,
    point_id: str = SUPPLY_POINT_ID,
    lifecycle: ResourceLifecycle = ResourceLifecycle.ACTIVE,
) -> OejpAccount:
    return OejpAccount(
        number=ACCOUNT_ID,
        lifecycle=ResourceLifecycle.ACTIVE,
        properties=(
            OejpProperty(
                id="PRIVATE-PROPERTY",
                supply_points=(
                    OejpSupplyPoint(
                        id=point_id,
                        account_number=ACCOUNT_ID,
                        direction=ReadingDirection.IMPORT,
                        lifecycle=lifecycle,
                    ),
                ),
            ),
        ),
    )


def _aggregation() -> SupplyPointAggregation:
    latest = AggregatedInterval(
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.IMPORT,
        start_at=NOW - timedelta(hours=1),
        end_at=NOW - timedelta(minutes=30),
        energy_kwh=Decimal("0.375"),
        official_cost=None,
        contributing_records=1,
        correction_count=2,
    )
    return SupplyPointAggregation(
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.IMPORT,
        latest=latest,
        today=PeriodAggregate(Decimal("1.25")),
        yesterday=PeriodAggregate(Decimal("4.5")),
        this_week=PeriodAggregate(Decimal("12.75")),
        this_month=PeriodAggregate(Decimal("48.25")),
        last_month=PeriodAggregate(Decimal("120.5")),
        latest_reading_end=latest.end_at,
        data_delay=timedelta(minutes=30),
    )


def _coordinator(
    *,
    accounts: tuple[OejpAccount, ...] | None = None,
    present: bool = True,
    queryable: bool = True,
) -> OejpDataUpdateCoordinator:
    coordinator = Mock()
    coordinator.accounts = accounts or (_account(),)
    coordinator.capabilities = CapabilitySnapshot()
    coordinator.last_update_success = True
    coordinator.data = OejpCoordinatorData(
        accounts=coordinator.accounts,
        capabilities=coordinator.capabilities,
        aggregation=AggregationSnapshot((_aggregation(),), NOW),
        present_supply_points=(
            frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}) if present else frozenset()
        ),
        direction_statuses=(
            (
                DirectionSyncStatus(
                    account_identity=stable_account_identity(SECRET, ACCOUNT_ID),
                    supply_point_identity=stable_supply_point_identity(
                        SECRET,
                        ACCOUNT_ID,
                        SUPPLY_POINT_ID,
                    ),
                    direction=ReadingDirection.IMPORT,
                    queryable=True,
                    last_success_at=NOW,
                ),
            )
            if queryable
            else ()
        ),
    )
    coordinator.async_add_listener.return_value = Mock()
    return cast("OejpDataUpdateCoordinator", coordinator)


def _description(key: str):
    return next(description for description in ENERGY_DESCRIPTIONS if description.key == key)


def test_consumption_sensors_project_typed_ledger_aggregates() -> None:
    coordinator = _coordinator()
    expected = {
        "latest_interval": Decimal("0.375"),
        "today": Decimal("1.25"),
        "yesterday": Decimal("4.5"),
        "this_week": Decimal("12.75"),
        "this_month": Decimal("48.25"),
        "last_month": Decimal("120.5"),
        "latest_reading": NOW - timedelta(minutes=30),
        "data_delay": 1800.0,
    }

    for key, value in expected.items():
        entity = OejpConsumptionSensor(
            coordinator,
            SECRET,
            ACCOUNT_ID,
            SUPPLY_POINT_ID,
            ReadingDirection.IMPORT,
            _description(key),
        )
        assert entity.native_value == value
        assert entity.translation_key == f"import_{key}"
        assert ACCOUNT_ID not in entity.unique_id
        assert SUPPLY_POINT_ID not in entity.unique_id
        assert entity.available


def test_sensor_returns_unknown_when_direction_has_no_readings() -> None:
    entity = OejpConsumptionSensor(
        _coordinator(),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.EXPORT,
        _description("today"),
    )
    assert entity.native_value is None


def test_supply_point_status_and_disappearance() -> None:
    active = OejpSupplyPointStatusSensor(
        _coordinator(),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
    )
    missing = OejpSupplyPointStatusSensor(
        _coordinator(present=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
    )

    assert active.native_value == ResourceLifecycle.ACTIVE
    assert active.available
    assert not missing.available


async def test_sensor_platform_adds_each_entity_once(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator()
    runtime = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=coordinator.accounts,
        capabilities=coordinator.capabilities,
        identity_secret=SECRET,
        coordinator=coordinator,
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = runtime
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    first_entities = add_entities.call_args.args[0]
    assert len(first_entities) == len(ENERGY_DESCRIPTIONS) + 1
    listener = cast("Mock", coordinator.async_add_listener).call_args.args[0]
    listener()
    assert add_entities.call_count == 1


async def test_capability_or_topology_alone_creates_only_status_sensor(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(queryable=False)
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
    assert isinstance(add_entities.call_args.args[0][0], OejpSupplyPointStatusSensor)
