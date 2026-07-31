"""Tests for OEJP coordinator synchronization and entity topology helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CapabilityStatus,
    EnergyReading,
    EnergyUnit,
    OejpAccount,
    OejpAuthenticationError,
    OejpProperty,
    OejpSupplyPoint,
    OejpTransportError,
    ReadingBatch,
    ReadingDirection,
    ReadingProviderName,
    ReadingSeriesKey,
    ReadingSource,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.const import (
    CONF_ENABLED_HISTORICAL_RESOURCES,
    DOMAIN,
)
from custom_components.octopus_energy_japan.coordinator import (
    OejpDataUpdateCoordinator,
    _SupplyPointRuntime,
    enabled_supply_points,
    entity_directions,
    iter_supply_points,
)
from custom_components.octopus_energy_japan.identity import (
    stable_account_identity,
    stable_supply_point_identity,
)
from custom_components.octopus_energy_japan.ledger import CorrectionResult, LedgerRecord
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
SECRET = "03" * 32
ACCOUNT_ID = "PRIVATE-ACCOUNT"
SUPPLY_POINT_ID = "PRIVATE-SUPPLY-POINT"


def _point(
    *,
    point_id: str = SUPPLY_POINT_ID,
    account_id: str = ACCOUNT_ID,
    lifecycle: ResourceLifecycle = ResourceLifecycle.ACTIVE,
    direction: ReadingDirection = ReadingDirection.IMPORT,
) -> OejpSupplyPoint:
    return OejpSupplyPoint(
        id=point_id,
        account_number=account_id,
        direction=direction,
        lifecycle=lifecycle,
    )


def _account(
    *points: OejpSupplyPoint,
    account_id: str = ACCOUNT_ID,
    lifecycle: ResourceLifecycle = ResourceLifecycle.ACTIVE,
) -> OejpAccount:
    return OejpAccount(
        number=account_id,
        lifecycle=lifecycle,
        properties=(OejpProperty(id="PRIVATE-PROPERTY", supply_points=points or (_point(),)),),
    )


def _capabilities(*capabilities: Capability) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        tuple(
            CapabilityStatus(capability, CapabilityAvailability.SUPPORTED)
            for capability in capabilities
        )
    )


def _reading() -> EnergyReading:
    return EnergyReading(
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.IMPORT,
        start_at=NOW - timedelta(hours=1),
        end_at=NOW - timedelta(minutes=30),
        value=Decimal("0.5"),
        unit=EnergyUnit.KWH,
        source=ReadingSource.SUPPLY_POINT_READINGS,
        fetched_at=NOW,
    )


def _entry(
    *,
    selected: list[str] | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_ENABLED_HISTORICAL_RESOURCES: selected or [],
        },
    )


def _coordinator(
    hass: HomeAssistant,
    *,
    entry: MockConfigEntry | None = None,
    accounts: tuple[OejpAccount, ...] | None = None,
    capabilities: CapabilitySnapshot | None = None,
    discovery_loader: AsyncMock | None = None,
) -> OejpDataUpdateCoordinator:
    selected_entry = entry or _entry()
    selected_accounts = accounts or (_account(),)
    selected_capabilities = capabilities or _capabilities(Capability.IMPORT_READINGS)
    return OejpDataUpdateCoordinator(
        hass,
        selected_entry,
        cast("Any", AsyncMock()),
        selected_accounts,
        selected_capabilities,
        SECRET,
        discovery_loader or AsyncMock(return_value=(selected_accounts, selected_capabilities)),
        now=lambda: NOW,
    )


def test_resource_helpers_never_select_only_the_first_item() -> None:
    active = _point()
    historical = _point(
        point_id="PRIVATE-HISTORICAL-POINT",
        lifecycle=ResourceLifecycle.HISTORICAL,
    )
    old_account_id = "PRIVATE-HISTORICAL-ACCOUNT"
    old_point = _point(
        point_id="PRIVATE-OLD-ACCOUNT-POINT",
        account_id=old_account_id,
        lifecycle=ResourceLifecycle.HISTORICAL,
    )
    historical_account = _account(
        old_point,
        account_id=old_account_id,
        lifecycle=ResourceLifecycle.HISTORICAL,
    )
    accounts = (_account(active, historical), historical_account)

    assert iter_supply_points(accounts[0]) == (active, historical)
    assert enabled_supply_points(_entry(), accounts, SECRET) == (active,)

    selected = [
        stable_supply_point_identity(
            SECRET,
            ACCOUNT_ID,
            historical.id,
        ),
        stable_account_identity(SECRET, historical_account.number),
        stable_supply_point_identity(
            SECRET,
            old_account_id,
            old_point.id,
        ),
    ]
    assert set(enabled_supply_points(_entry(selected=selected), accounts, SECRET)) == {
        active,
        historical,
        old_point,
    }


def test_entity_directions_use_topology_and_capabilities() -> None:
    assert entity_directions(_point(), CapabilitySnapshot()) == (ReadingDirection.IMPORT,)
    assert entity_directions(
        _point(direction=ReadingDirection.UNKNOWN),
        _capabilities(Capability.EXPORT_READINGS, Capability.IMPORT_READINGS),
    ) == (ReadingDirection.EXPORT, ReadingDirection.IMPORT)
    assert entity_directions(
        _point(direction=ReadingDirection.UNKNOWN),
        CapabilitySnapshot(),
    ) == (ReadingDirection.IMPORT,)


async def test_update_reconciles_window_and_projects_ledger(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    reading = _reading()
    series = ReadingSeriesKey.from_reading(reading)
    router = AsyncMock()
    router.async_get_readings.return_value = ReadingBatch(
        readings=(reading,),
        provider=ReadingProviderName.GENERIC,
        observed_at=NOW,
        authoritative_series=frozenset({series}),
        authoritative_sources=frozenset({ReadingSource.SUPPLY_POINT_READINGS}),
    )
    ledger = AsyncMock()
    ledger.corrupt_partitions = frozenset()
    ledger.async_records.side_effect = [(), (LedgerRecord(reading),)]
    ledger.async_reconcile.return_value = CorrectionResult()
    backend = AsyncMock()
    coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)] = _SupplyPointRuntime(
        supply_point=_point(),
        backend=backend,
        ledger=ledger,
        router=router,
    )
    coordinator._first_sync = False
    coordinator._schedule = coordinator._schedule.__class__(
        last_reconciliation_date=NOW.date(),
        last_discovery_at=NOW,
    )
    coordinator._async_prepare_enabled_supply_points = AsyncMock()  # type: ignore[method-assign]

    data = await coordinator._async_update_data()

    router.async_get_readings.assert_awaited_once()
    ledger.async_reconcile.assert_awaited_once()
    assert data.aggregation.supply_points[0].today.energy_kwh == Decimal("0.5")
    assert data.provider_observations[0].provider is ReadingProviderName.GENERIC
    assert data.present_supply_points == {(ACCOUNT_ID, SUPPLY_POINT_ID)}
    await coordinator.async_shutdown_runtime()
    backend.async_flush.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OejpAuthenticationError(()), ConfigEntryAuthFailed),
        (OejpTransportError("offline"), UpdateFailed),
    ],
)
async def test_update_normalizes_runtime_failures(
    hass: HomeAssistant,
    error: Exception,
    expected: type[Exception],
) -> None:
    coordinator = _coordinator(hass)
    router = AsyncMock()
    router.async_get_readings.side_effect = error
    ledger = AsyncMock()
    ledger.corrupt_partitions = frozenset()
    coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)] = _SupplyPointRuntime(
        supply_point=_point(),
        backend=AsyncMock(),
        ledger=ledger,
        router=router,
    )
    coordinator._first_sync = False
    coordinator._schedule = coordinator._schedule.__class__(
        last_reconciliation_date=NOW.date(),
        last_discovery_at=NOW,
    )
    coordinator._async_prepare_enabled_supply_points = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(expected):
        await coordinator._async_update_data()


async def test_prepare_initializes_each_enabled_supply_point_once(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    backend = Mock()
    ledger = Mock()
    ledger.async_initialize = AsyncMock()
    with (
        patch(
            "custom_components.octopus_energy_japan.coordinator.HomeAssistantLedgerBackend",
            return_value=backend,
        ) as backend_type,
        patch(
            "custom_components.octopus_energy_japan.coordinator.PersistentIntervalLedger",
            return_value=ledger,
        ) as ledger_type,
        patch.object(coordinator, "_reading_router", return_value=Mock()),
    ):
        await coordinator._async_prepare_enabled_supply_points(NOW)
        await coordinator._async_prepare_enabled_supply_points(NOW)

    backend_type.assert_called_once()
    ledger_type.assert_called_once_with(
        backend,
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
    )
    ledger.async_initialize.assert_awaited_once_with(NOW)
