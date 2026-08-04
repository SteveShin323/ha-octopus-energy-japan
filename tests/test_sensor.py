"""Tests for privacy-preserving OEJP sensor projections."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.octopus_energy_japan.aggregation import (
    AggregatedInterval,
    AggregationSnapshot,
    PeriodAggregate,
    SupplyPointAggregation,
)
from custom_components.octopus_energy_japan.api import (
    AccountCommercialSnapshot,
    AccountOverview,
    AgreementSummary,
    BillSummary,
    CapabilitySnapshot,
    CommercialAccess,
    CommercialAvailability,
    CommercialFeature,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
    ProductSummary,
    ReadingDirection,
    ResourceLifecycle,
    TransactionSummary,
)
from custom_components.octopus_energy_japan.commercial_coordinator import (
    OejpCommercialCoordinator,
    OejpCommercialData,
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
    COMMERCIAL_DESCRIPTIONS,
    COST_DESCRIPTIONS,
    ENERGY_DESCRIPTIONS,
    OejpAccountCommercialSensor,
    OejpConsumptionSensor,
    OejpSupplyPointStatusSensor,
    async_setup_entry,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady
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
        today=PeriodAggregate(Decimal("1.25"), Decimal("41"), complete=True),
        yesterday=PeriodAggregate(Decimal("4.5"), complete=True),
        this_week=PeriodAggregate(Decimal("12.75"), complete=True),
        this_month=PeriodAggregate(Decimal("48.25"), Decimal("1580"), complete=True),
        last_month=PeriodAggregate(Decimal("120.5"), complete=True),
        latest_reading_end=latest.end_at,
        data_delay=timedelta(minutes=30),
    )


def _coordinator(
    *,
    accounts: tuple[OejpAccount, ...] | None = None,
    present: bool = True,
    queryable: bool = True,
    enabled: bool = True,
    stale: bool = False,
    update_success: bool = True,
) -> OejpDataUpdateCoordinator:
    coordinator = Mock()
    coordinator.accounts = accounts or (_account(),)
    coordinator.capabilities = CapabilitySnapshot()
    coordinator.last_update_success = update_success
    coordinator.data = OejpCoordinatorData(
        accounts=coordinator.accounts,
        capabilities=coordinator.capabilities,
        aggregation=AggregationSnapshot((_aggregation(),), NOW),
        present_supply_points=(
            frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}) if present else frozenset()
        ),
        enabled_supply_points=(
            frozenset({(ACCOUNT_ID, SUPPLY_POINT_ID)}) if enabled else frozenset()
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
                    stale=stale,
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
    return next(
        description
        for description in (*ENERGY_DESCRIPTIONS, *COST_DESCRIPTIONS)
        if description.key == key
    )


def _commercial_description(key: str):
    return next(description for description in COMMERCIAL_DESCRIPTIONS if description.key == key)


def _commercial_snapshot(
    *,
    availability: CommercialAvailability = CommercialAvailability.AVAILABLE,
    agreements: tuple[AgreementSummary, ...] | None = None,
) -> AccountCommercialSnapshot:
    return AccountCommercialSnapshot(
        ACCOUNT_ID,
        overview=AccountOverview(ACCOUNT_ID, "ACTIVE", 1234, 50, True, False),
        agreements=(
            agreements
            if agreements is not None
            else (
                AgreementSummary(
                    "agreement",
                    NOW - timedelta(days=30),
                    NOW + timedelta(days=30),
                    None,
                    None,
                    True,
                    ProductSummary(
                        "product",
                        "PRODUCT-CODE",
                        "Octopus plan",
                        None,
                        "ELECTRICITY",
                    ),
                ),
            )
        ),
        latest_bill=BillSummary(
            id="bill",
            type_name="StatementType",
            bill_type="STATEMENT",
            from_date=date(2026, 6, 1),
            to_date=date(2026, 6, 30),
            issued_date=date(2026, 7, 3),
            due_date=date(2026, 7, 27),
            gross_amount_minor=8640,
            status="CLOSED",
            is_annulled=False,
            is_held=False,
        ),
        latest_transaction=TransactionSummary(
            id="transaction",
            type_name="Payment",
            posted_date=date(2026, 7, 27),
            created_at=NOW - timedelta(days=2),
            amount_minor=-8640,
            is_held=False,
            is_issued=True,
            is_reversed=False,
            reason_code=None,
        ),
        access=tuple(CommercialAccess(feature, availability) for feature in CommercialFeature),
        observed_at=NOW,
    )


def _commercial_coordinator(
    *,
    availability: CommercialAvailability = CommercialAvailability.AVAILABLE,
    snapshots: tuple[AccountCommercialSnapshot, ...] | None = None,
) -> OejpCommercialCoordinator:
    coordinator = Mock()
    coordinator.last_update_success = True
    coordinator.data = OejpCommercialData(
        snapshots if snapshots is not None else (_commercial_snapshot(availability=availability),),
        NOW,
    )
    coordinator.async_add_listener.return_value = Mock()
    return cast("OejpCommercialCoordinator", coordinator)


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


def test_commercial_sensors_project_bounded_account_values_without_raw_ids() -> None:
    coordinator = _commercial_coordinator()
    expected = {
        "account_status": "ACTIVE",
        "current_product": "Octopus plan",
        "agreement_start": NOW - timedelta(days=30),
        "agreement_end": NOW + timedelta(days=30),
        "account_balance": 1234,
        "overdue_balance": 50,
        "latest_bill_amount": 8640,
        "latest_bill_issued": date(2026, 7, 3),
        "latest_bill_due": date(2026, 7, 27),
        "latest_transaction_amount": -8640,
    }
    assert set(expected) == {description.key for description in COMMERCIAL_DESCRIPTIONS}

    for key, value in expected.items():
        entity = OejpAccountCommercialSensor(
            coordinator,
            SECRET,
            ACCOUNT_ID,
            _commercial_description(key),
        )
        assert entity.native_value == value
        assert entity.translation_key == key
        assert ACCOUNT_ID not in entity.unique_id
        assert entity.available


def test_financial_commercial_entities_are_disabled_by_default() -> None:
    disabled = {
        description.key
        for description in COMMERCIAL_DESCRIPTIONS
        if not description.entity_registry_enabled_default
    }

    assert disabled == {
        "account_balance",
        "overdue_balance",
        "latest_bill_amount",
        "latest_bill_issued",
        "latest_bill_due",
        "latest_transaction_amount",
    }
    assert all(not description.entity_registry_enabled_default for description in COST_DESCRIPTIONS)


def test_official_cost_sensors_report_only_fully_covered_periods() -> None:
    coordinator = _coordinator()
    expected = {
        "official_cost_today": Decimal("41"),
        "official_cost_this_month": Decimal("1580"),
    }
    assert set(expected) == {description.key for description in COST_DESCRIPTIONS}

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

    aggregate = _aggregation()
    incomplete = replace(
        aggregate,
        today=replace(aggregate.today, complete=False),
        this_month=replace(aggregate.this_month, complete=False),
    )
    coordinator.data = replace(
        coordinator.data,
        aggregation=AggregationSnapshot((incomplete,), NOW),
    )

    for key in expected:
        entity = OejpConsumptionSensor(
            coordinator,
            SECRET,
            ACCOUNT_ID,
            SUPPLY_POINT_ID,
            ReadingDirection.IMPORT,
            _description(key),
        )
        assert entity.native_value is None


def test_commercial_sensor_is_unknown_for_an_account_without_a_snapshot() -> None:
    entity = OejpAccountCommercialSensor(
        _commercial_coordinator(snapshots=()),
        SECRET,
        ACCOUNT_ID,
        _commercial_description("account_status"),
    )

    assert entity.native_value is None
    assert not entity.available


def test_agreement_projections_are_unknown_without_a_current_agreement() -> None:
    coordinator = _commercial_coordinator(
        snapshots=(_commercial_snapshot(agreements=()),),
    )

    for key in ("current_product", "agreement_start", "agreement_end"):
        entity = OejpAccountCommercialSensor(
            coordinator,
            SECRET,
            ACCOUNT_ID,
            _commercial_description(key),
        )
        assert entity.native_value is None


def test_current_product_is_unknown_when_the_agreement_has_no_product() -> None:
    entity = OejpAccountCommercialSensor(
        _commercial_coordinator(
            snapshots=(
                _commercial_snapshot(
                    agreements=(
                        AgreementSummary(
                            "agreement",
                            NOW - timedelta(days=30),
                            None,
                            None,
                            None,
                            True,
                            None,
                        ),
                    ),
                ),
            ),
        ),
        SECRET,
        ACCOUNT_ID,
        _commercial_description("current_product"),
    )

    assert entity.native_value is None


def test_commercial_sensor_is_unavailable_when_optional_permission_is_missing() -> None:
    entity = OejpAccountCommercialSensor(
        _commercial_coordinator(availability=CommercialAvailability.FORBIDDEN),
        SECRET,
        ACCOUNT_ID,
        _commercial_description("account_balance"),
    )

    assert not entity.available


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


def test_period_sensor_is_unknown_until_authoritative_coverage_is_complete() -> None:
    coordinator = _coordinator()
    aggregate = _aggregation()
    incomplete = replace(aggregate, this_month=replace(aggregate.this_month, complete=False))
    coordinator.data = replace(
        coordinator.data,
        aggregation=AggregationSnapshot((incomplete,), NOW),
    )
    entity = OejpConsumptionSensor(
        coordinator,
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
        _description("this_month"),
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
    reading_refresh_failed = OejpSupplyPointStatusSensor(
        _coordinator(update_success=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
    )
    failed_direction = OejpConsumptionSensor(
        _coordinator(update_success=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
        _description("latest_interval"),
    )

    assert active.native_value == ResourceLifecycle.ACTIVE
    assert active.available
    assert reading_refresh_failed.available
    assert not failed_direction.available
    assert not missing.available


def test_disabled_nonqueryable_and_stale_directions_are_unavailable() -> None:
    disabled_status = OejpSupplyPointStatusSensor(
        _coordinator(enabled=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
    )
    nonqueryable_energy = OejpConsumptionSensor(
        _coordinator(queryable=False),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
        _description("latest_interval"),
    )
    stale_energy = OejpConsumptionSensor(
        _coordinator(stale=True),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.IMPORT,
        _description("latest_interval"),
    )

    assert not disabled_status.available
    assert not nonqueryable_energy.available
    assert not stale_energy.available


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
    assert len(first_entities) == len(ENERGY_DESCRIPTIONS) + len(COST_DESCRIPTIONS) + 1
    listener = cast("Mock", coordinator.async_add_listener).call_args.args[0]
    listener()
    assert add_entities.call_count == 1


async def test_sensor_platform_adds_commercial_entities_once_per_account(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator()
    commercial_coordinator = _commercial_coordinator()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=coordinator.accounts,
        capabilities=coordinator.capabilities,
        identity_secret=SECRET,
        coordinator=coordinator,
        commercial_coordinator=commercial_coordinator,
    )
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert add_entities.call_count == 2
    assert len(add_entities.call_args.args[0]) == len(COMMERCIAL_DESCRIPTIONS)
    assert all(
        isinstance(entity, OejpAccountCommercialSensor) for entity in add_entities.call_args.args[0]
    )
    listener = cast("Mock", commercial_coordinator.async_add_listener).call_args.args[0]
    listener()
    assert add_entities.call_count == 2


async def test_sensor_platform_requires_a_runtime_coordinator(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = None

    with pytest.raises(PlatformNotReady):
        await async_setup_entry(hass, entry, Mock())


async def test_sensor_platform_refuses_an_entity_without_a_unique_id(
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

    with (
        patch.object(OejpSupplyPointStatusSensor, "unique_id", None),
        pytest.raises(RuntimeError, match="unique ID"),
    ):
        await async_setup_entry(hass, entry, Mock())


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


async def test_direction_entities_are_added_dynamically_exactly_once(
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
    listener = cast("Mock", coordinator.async_add_listener).call_args.args[0]

    coordinator.data = replace(
        coordinator.data,
        direction_statuses=(
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
        ),
    )
    listener()
    listener()

    assert add_entities.call_count == 2
    assert len(add_entities.call_args.args[0]) == len(ENERGY_DESCRIPTIONS) + len(COST_DESCRIPTIONS)
