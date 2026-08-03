"""Tests for OEJP coordinator synchronization and entity topology helpers."""

from __future__ import annotations

import asyncio
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
    DirectionReadingResult,
    EnergyReading,
    EnergyUnit,
    OejpAccount,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpError,
    OejpInvalidResponseError,
    OejpNonRetryableHttpError,
    OejpNoReadingProviderError,
    OejpNotFoundError,
    OejpProperty,
    OejpQueryValidationError,
    OejpRateLimitError,
    OejpSupplyPoint,
    OejpTimeoutError,
    OejpTransientHttpError,
    OejpTransportError,
    ReadingDirection,
    ReadingProviderName,
    ReadingSeriesKey,
    ReadingSource,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.background_sync import (
    BackgroundSyncItem,
    BackgroundSyncReason,
    BackgroundSyncScope,
    BackgroundWindow,
    CoverageWindow,
    PlannedGeneration,
    SyncCheckpoint,
    SyncObligation,
)
from custom_components.octopus_energy_japan.const import (
    CONF_ENABLED_HISTORICAL_RESOURCES,
    DOMAIN,
)
from custom_components.octopus_energy_japan.coordinator import (
    DirectionErrorClass,
    DirectionSyncStatus,
    OejpDataUpdateCoordinator,
    _StatisticsPending,
    _SupplyPointRuntime,
    enabled_supply_points,
    entity_directions,
    iter_supply_points,
)
from custom_components.octopus_energy_japan.identity import (
    stable_account_identity,
    stable_supply_point_identity,
)
from custom_components.octopus_energy_japan.ledger import (
    CorrectionResult,
    LedgerChange,
    LedgerError,
    LedgerMergeStatus,
    LedgerRecord,
)
from custom_components.octopus_energy_japan.statistics_runtime import StatisticsProjector
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


def _direction_result(
    direction: ReadingDirection,
    *readings: EnergyReading,
    point: OejpSupplyPoint | None = None,
) -> DirectionReadingResult:
    account_id = point.account_number if point is not None else ACCOUNT_ID
    supply_point_id = point.id if point is not None else SUPPLY_POINT_ID
    authoritative_series = frozenset(
        ReadingSeriesKey.from_reading(reading) for reading in readings
    ) or frozenset(
        {
            ReadingSeriesKey(
                account_id=account_id,
                supply_point_id=supply_point_id,
                direction=direction,
                unit=EnergyUnit.KWH,
                source=ReadingSource.SUPPLY_POINT_READINGS,
            )
        }
    )
    return DirectionReadingResult(
        readings=readings,
        direction=direction,
        provider=ReadingProviderName.GENERIC,
        observed_at=NOW,
        authoritative_series=authoritative_series,
        authoritative_sources=frozenset({ReadingSource.SUPPLY_POINT_READINGS}),
    )


def _install_state(
    coordinator: OejpDataUpdateCoordinator,
    point: OejpSupplyPoint,
    *,
    router: AsyncMock,
    records: tuple[LedgerRecord, ...] = (),
) -> tuple[AsyncMock, AsyncMock]:
    ledger = AsyncMock()
    ledger.corrupt_partitions = frozenset()
    ledger.async_records.return_value = records
    ledger.async_reconcile.return_value = CorrectionResult()
    backend = AsyncMock()
    coordinator._supply_points[(point.account_number, point.id)] = _SupplyPointRuntime(
        supply_point=point,
        backend=backend,
        ledger=ledger,
        router=router,
        checkpoint_backend=AsyncMock(),
        checkpoint=SyncCheckpoint.empty(NOW),
    )
    coordinator._async_prepare_enabled_supply_points = AsyncMock()  # type: ignore[method-assign]
    return ledger, backend


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
    statistics_projector: StatisticsProjector | None = None,
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
        startup_delay=timedelta(0),
        statistics_projector=statistics_projector,
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
    ]
    assert set(enabled_supply_points(_entry(selected=selected), accounts, SECRET)) == {
        active,
        historical,
        old_point,
    }


async def test_statistics_projection_flushes_ledger_and_clears_pending(
    hass: HomeAssistant,
) -> None:
    projector = AsyncMock()
    coordinator = _coordinator(
        hass,
        statistics_projector=cast("StatisticsProjector", projector),
    )
    point = _point()
    ledger, backend = _install_state(coordinator, point, router=AsyncMock())
    key = (ACCOUNT_ID, SUPPLY_POINT_ID)
    coordinator._statistics_pending[key] = _StatisticsPending(None)

    await coordinator._async_publish_pending_statistics(NOW)

    backend.async_flush.assert_awaited_once_with()
    projector.async_project_supply_point.assert_awaited_once_with(
        ledger,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        NOW,
        dirty_from=None,
        reset_directions=frozenset(),
    )
    assert key not in coordinator._statistics_pending


async def test_statistics_projection_failure_is_retried_and_recovers(
    hass: HomeAssistant,
) -> None:
    projector = AsyncMock()
    projector.async_project_supply_point.side_effect = [OSError("recorder offline"), None]
    coordinator = _coordinator(
        hass,
        statistics_projector=cast("StatisticsProjector", projector),
    )
    point = _point()
    _ledger, backend = _install_state(coordinator, point, router=AsyncMock())
    key = (ACCOUNT_ID, SUPPLY_POINT_ID)
    coordinator._statistics_pending[key] = _StatisticsPending(NOW - timedelta(hours=2))

    await coordinator._async_publish_pending_statistics(NOW)

    assert key in coordinator._statistics_pending
    assert key in coordinator._statistics_failures

    await coordinator._async_publish_pending_statistics(NOW)

    assert key not in coordinator._statistics_pending
    assert key not in coordinator._statistics_failures
    assert backend.async_flush.await_count == 2


async def test_statistics_success_for_another_point_does_not_mask_failure(
    hass: HomeAssistant,
) -> None:
    projector = AsyncMock()
    projector.async_project_supply_point.side_effect = [OSError("recorder offline"), None]
    coordinator = _coordinator(
        hass,
        statistics_projector=cast("StatisticsProjector", projector),
    )
    first_key = (ACCOUNT_ID, SUPPLY_POINT_ID)
    second_key = (ACCOUNT_ID, "PRIVATE-SUPPLY-POINT-2")
    _install_state(coordinator, _point(), router=AsyncMock())
    _install_state(
        coordinator,
        _point(point_id=second_key[1]),
        router=AsyncMock(),
    )
    coordinator._statistics_pending[first_key] = _StatisticsPending(NOW)
    coordinator._statistics_pending[second_key] = _StatisticsPending(NOW)

    await coordinator._async_publish_pending_statistics(NOW)

    assert first_key in coordinator._statistics_pending
    assert first_key in coordinator._statistics_failures
    assert second_key not in coordinator._statistics_pending
    assert second_key not in coordinator._statistics_failures


def test_statistics_dirty_state_retains_earliest_change(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    key = (ACCOUNT_ID, SUPPLY_POINT_ID)
    record = LedgerRecord(_reading())
    later = NOW - timedelta(minutes=30)
    coordinator._statistics_pending[key] = _StatisticsPending(later)

    coordinator._mark_statistics_dirty(
        CorrectionResult(
            (
                LedgerChange(
                    record.key,
                    LedgerMergeStatus.CORRECTED,
                    previous=record,
                    current=record,
                ),
            )
        )
    )

    assert coordinator._statistics_pending[key].dirty_from == record.key.start_at


def test_statistics_deletion_requests_direction_reset(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    _install_state(coordinator, _point(), router=AsyncMock())
    record = LedgerRecord(_reading())

    coordinator._mark_statistics_dirty(
        CorrectionResult(
            (
                LedgerChange(
                    record.key,
                    LedgerMergeStatus.DELETED,
                    previous=record,
                ),
            )
        )
    )

    pending = coordinator._statistics_pending[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    assert pending.dirty_from == record.key.start_at
    assert pending.reset_directions == frozenset({ReadingDirection.IMPORT})


@pytest.mark.parametrize(
    "discovered_accounts",
    [
        (),
        (
            _account(
                _point(lifecycle=ResourceLifecycle.HISTORICAL),
                lifecycle=ResourceLifecycle.HISTORICAL,
            ),
        ),
    ],
)
async def test_discovery_cancels_disabled_or_missing_work_but_retains_store(
    hass: HomeAssistant,
    discovered_accounts: tuple[OejpAccount, ...],
) -> None:
    capabilities = _capabilities(Capability.IMPORT_READINGS)
    loader = AsyncMock(return_value=(discovered_accounts, capabilities))
    coordinator = _coordinator(hass, discovery_loader=loader)
    coordinator._entry.runtime_data = None
    coordinator._schedule = coordinator._schedule.__class__(
        last_discovery_at=NOW - timedelta(hours=24),
    )
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    coordinator._background_queue.enqueue(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test"),
    )

    await coordinator._async_refresh_discovery_if_due(NOW)

    assert coordinator._background_queue.snapshot() == ()
    assert coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)] is state


async def test_daily_generation_reconsiders_permanent_background_failure(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    generation = PlannedGeneration(obligation, window.end_at, (window,))
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    state.checkpoint = state.checkpoint.register(generation).mark_failed(
        item,
        DirectionErrorClass.AUTHORIZATION.value,
    )
    coordinator._record_direction_success(
        state,
        ReadingDirection.IMPORT,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )
    coordinator._schedule = coordinator._schedule.__class__(last_discovery_at=NOW)

    await coordinator._async_schedule_background_work_locked(NOW)

    assert state.checkpoint.failed_windows == ()
    assert item in coordinator._background_queue.snapshot()
    state.checkpoint_backend.async_save.assert_awaited_once()


async def test_permanent_background_failure_is_not_retried_by_every_poll(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    generation = PlannedGeneration(obligation, window.end_at, (window,))
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    state.checkpoint = state.checkpoint.register(generation).mark_failed(
        item,
        DirectionErrorClass.AUTHORIZATION.value,
    )
    coordinator._record_direction_success(
        state,
        ReadingDirection.IMPORT,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )
    coordinator._schedule = coordinator._schedule.__class__(
        last_reconciliation_date=NOW.date(),
        last_discovery_at=NOW,
    )

    await coordinator._async_schedule_background_work_locked(NOW)

    assert state.checkpoint.failed_windows
    assert coordinator._background_queue.snapshot() == ()
    state.checkpoint_backend.async_save.assert_not_awaited()


async def test_discovery_generation_reconsiders_relevant_permanent_failure(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    ).mark_failed(item, DirectionErrorClass.AUTHORIZATION.value)

    await coordinator._async_reconsider_failures_after_discovery()

    assert state.checkpoint.failed_windows == ()
    state.checkpoint_backend.async_save.assert_awaited_once()


def test_entity_directions_require_authoritative_direction_success() -> None:
    assert entity_directions(None, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID) == ()
    data = Mock(
        direction_statuses=(
            DirectionSyncStatus(
                account_identity=stable_account_identity(SECRET, ACCOUNT_ID),
                supply_point_identity=stable_supply_point_identity(
                    SECRET,
                    ACCOUNT_ID,
                    SUPPLY_POINT_ID,
                ),
                direction=ReadingDirection.EXPORT,
                queryable=True,
                last_success_at=NOW,
            ),
        )
    )
    assert entity_directions(
        cast("Any", data),
        SECRET,
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
    ) == (ReadingDirection.EXPORT,)
    assert (
        entity_directions(
            cast("Any", data),
            SECRET,
            ACCOUNT_ID,
            "OTHER-POINT",
        )
        == ()
    )


async def test_update_reconciles_window_and_projects_ledger(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    reading = _reading()
    series = ReadingSeriesKey.from_reading(reading)
    router = AsyncMock()
    router.async_get_readings.return_value = DirectionReadingResult(
        readings=(reading,),
        direction=ReadingDirection.IMPORT,
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
        checkpoint_backend=AsyncMock(),
        checkpoint=SyncCheckpoint.empty(NOW),
    )
    coordinator._schedule = coordinator._schedule.__class__(
        last_reconciliation_date=NOW.date(),
        last_discovery_at=NOW,
    )
    coordinator._async_prepare_enabled_supply_points = AsyncMock()  # type: ignore[method-assign]

    data = await coordinator._async_update_data()

    router.async_get_readings.assert_awaited_once()
    assert coordinator._background_queue.snapshot() == ()
    assert router.async_get_readings.await_args.args[1] is ReadingDirection.IMPORT
    assert router.async_get_readings.await_args.args[2:] == (
        NOW - timedelta(hours=72),
        NOW,
    )
    ledger.async_reconcile.assert_awaited_once()
    assert data.aggregation.supply_points[0].today.energy_kwh == Decimal("0.5")
    assert data.aggregation.supply_points[0].today.complete
    assert data.aggregation.supply_points[0].yesterday.complete
    assert data.aggregation.supply_points[0].this_week.complete
    assert not data.aggregation.supply_points[0].this_month.complete
    assert not data.aggregation.supply_points[0].last_month.complete
    assert data.provider_observations[0].provider is ReadingProviderName.GENERIC
    assert data.provider_observations[0].direction is ReadingDirection.IMPORT
    assert data.direction_statuses[0].queryable
    assert data.direction_statuses[0].coverage_start_at == NOW - timedelta(hours=72)
    assert data.direction_statuses[0].coverage_end_at == NOW
    assert (
        data.direction_status(
            stable_account_identity(SECRET, ACCOUNT_ID),
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
        )
        is data.direction_statuses[0]
    )
    assert (
        data.direction_status(
            "missing-account",
            "missing-point",
            ReadingDirection.IMPORT,
        )
        is None
    )
    assert data.present_supply_points == {(ACCOUNT_ID, SUPPLY_POINT_ID)}
    await coordinator.async_shutdown_runtime()
    backend.async_flush.assert_awaited_once_with()


async def test_regular_refresh_schedules_history_only_after_background_start(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._accounts = ()
    coordinator._background_started = True
    coordinator._async_schedule_background_work = AsyncMock()  # type: ignore[method-assign]

    await coordinator._async_update_data()

    coordinator._async_schedule_background_work.assert_awaited_once_with(NOW)


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
        checkpoint_backend=AsyncMock(),
        checkpoint=SyncCheckpoint.empty(NOW),
    )
    coordinator._schedule = coordinator._schedule.__class__(
        last_reconciliation_date=NOW.date(),
        last_discovery_at=NOW,
    )
    coordinator._async_prepare_enabled_supply_points = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(expected):
        await coordinator._async_update_data()


@pytest.mark.parametrize(
    ("error", "error_class"),
    [
        (OejpAuthorizationError(()), DirectionErrorClass.AUTHORIZATION),
        (OejpQueryValidationError(()), DirectionErrorClass.VALIDATION),
        (OejpNotFoundError(()), DirectionErrorClass.NOT_FOUND),
        (OejpNonRetryableHttpError(400), DirectionErrorClass.NON_RETRYABLE_HTTP),
        (OejpNoReadingProviderError("unavailable"), DirectionErrorClass.UNAVAILABLE),
        (LedgerError("ledger failed"), DirectionErrorClass.LEDGER),
        (ValueError("invalid reading"), DirectionErrorClass.INVALID_RESPONSE),
    ],
)
async def test_permanent_only_refresh_publishes_status_without_direction(
    hass: HomeAssistant,
    error: Exception,
    error_class: DirectionErrorClass,
) -> None:
    coordinator = _coordinator(hass)
    router = AsyncMock()
    router.async_get_readings.side_effect = error
    _install_state(coordinator, _point(), router=router)

    data = await coordinator._async_update_data()

    assert data.aggregation.supply_points == ()
    assert len(data.direction_statuses) == 1
    status = data.direction_statuses[0]
    assert not status.queryable
    assert not status.stale
    assert status.error_class is error_class
    assert entity_directions(data, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID) == ()


async def test_no_enabled_supply_points_publishes_empty_snapshot(
    hass: HomeAssistant,
) -> None:
    historical = _point(lifecycle=ResourceLifecycle.HISTORICAL)
    coordinator = _coordinator(
        hass,
        accounts=(
            _account(
                historical,
                lifecycle=ResourceLifecycle.HISTORICAL,
            ),
        ),
    )

    data = await coordinator._async_update_data()

    assert data.enabled_supply_points == frozenset()
    assert data.direction_statuses == ()
    assert data.aggregation.supply_points == ()


async def test_successful_empty_export_is_queryable_with_import(
    hass: HomeAssistant,
) -> None:
    point = _point(direction=ReadingDirection.UNKNOWN)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    reading = _reading()
    router = AsyncMock()
    router.async_get_readings.side_effect = (
        _direction_result(ReadingDirection.EXPORT),
        _direction_result(ReadingDirection.IMPORT, reading),
    )
    ledger, _backend = _install_state(
        coordinator,
        point,
        router=router,
        records=(LedgerRecord(reading),),
    )

    data = await coordinator._async_update_data()

    assert [call.args[1] for call in router.async_get_readings.await_args_list] == [
        ReadingDirection.EXPORT,
        ReadingDirection.IMPORT,
    ]
    assert ledger.async_reconcile.await_count == 2
    assert entity_directions(data, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID) == (
        ReadingDirection.EXPORT,
        ReadingDirection.IMPORT,
    )
    assert len(data.aggregation.supply_points) == 2
    export = data.supply_point_aggregation(
        ACCOUNT_ID,
        SUPPLY_POINT_ID,
        ReadingDirection.EXPORT,
    )
    assert export is not None and export.latest is None


async def test_partial_refresh_preserves_success_when_later_direction_is_forbidden(
    hass: HomeAssistant,
) -> None:
    point = _point(direction=ReadingDirection.UNKNOWN)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    reading = _reading()
    router = AsyncMock()
    router.async_get_readings.side_effect = (
        OejpAuthorizationError(()),
        _direction_result(ReadingDirection.IMPORT, reading),
    )
    _install_state(
        coordinator,
        point,
        router=router,
        records=(LedgerRecord(reading),),
    )

    data = await coordinator._async_update_data()

    statuses = {status.direction: status for status in data.direction_statuses}
    assert statuses[ReadingDirection.EXPORT].error_class is DirectionErrorClass.AUTHORIZATION
    assert not statuses[ReadingDirection.EXPORT].queryable
    assert statuses[ReadingDirection.IMPORT].queryable
    assert data.aggregation.supply_points[0].direction is ReadingDirection.IMPORT


@pytest.mark.parametrize(
    ("error", "error_class"),
    [
        (OejpTransportError("offline"), DirectionErrorClass.TRANSIENT),
        (OejpRateLimitError(()), DirectionErrorClass.RATE_LIMIT),
    ],
)
async def test_shared_transient_stops_poll_and_keeps_previous_queryability_stale(
    hass: HomeAssistant,
    error: OejpTransportError | OejpRateLimitError,
    error_class: DirectionErrorClass,
) -> None:
    point_a = _point(point_id="PRIVATE-POINT-A")
    point_b = _point(point_id="PRIVATE-POINT-B")
    coordinator = _coordinator(hass, accounts=(_account(point_b, point_a),))
    router_a = AsyncMock()
    router_a.async_get_readings.side_effect = error
    router_b = AsyncMock()
    _install_state(coordinator, point_a, router=router_a)
    _install_state(coordinator, point_b, router=router_b)
    coordinator._ensure_direction_status(
        coordinator._supply_points[(ACCOUNT_ID, point_a.id)],
        ReadingDirection.IMPORT,
    )
    coordinator._record_direction_success(
        coordinator._supply_points[(ACCOUNT_ID, point_a.id)],
        ReadingDirection.IMPORT,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    router_a.async_get_readings.assert_awaited_once()
    router_b.async_get_readings.assert_not_awaited()
    status = coordinator._direction_statuses[(ACCOUNT_ID, point_a.id, ReadingDirection.IMPORT)]
    assert status.queryable
    assert status.stale
    assert status.error_class is error_class
    pending = coordinator._direction_statuses[(ACCOUNT_ID, point_b.id, ReadingDirection.IMPORT)]
    assert pending.error_class is error_class


async def test_aggregation_excludes_nonqueryable_direction_records(
    hass: HomeAssistant,
) -> None:
    point = _point(direction=ReadingDirection.UNKNOWN)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    import_reading = _reading()
    export_reading = EnergyReading(
        account_id=ACCOUNT_ID,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.EXPORT,
        start_at=import_reading.start_at,
        end_at=import_reading.end_at,
        value=Decimal("0.75"),
        unit=EnergyUnit.KWH,
        source=ReadingSource.SUPPLY_POINT_READINGS,
        fetched_at=NOW,
    )
    router = AsyncMock()
    router.async_get_readings.side_effect = (
        OejpAuthorizationError(()),
        _direction_result(ReadingDirection.IMPORT, import_reading),
    )
    _install_state(
        coordinator,
        point,
        router=router,
        records=(LedgerRecord(import_reading), LedgerRecord(export_reading)),
    )

    data = await coordinator._async_update_data()

    assert tuple(value.direction for value in data.aggregation.supply_points) == (
        ReadingDirection.IMPORT,
    )


async def test_aggregation_ledger_failure_isolated_to_its_point(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    router = AsyncMock()
    router.async_get_readings.return_value = _direction_result(ReadingDirection.IMPORT)
    ledger, _backend = _install_state(coordinator, _point(), router=router)
    ledger.async_records.side_effect = ((), LedgerError("failed"))

    data = await coordinator._async_update_data()

    assert data.aggregation.supply_points == ()
    assert data.direction_statuses[0].error_class is DirectionErrorClass.LEDGER
    assert not data.direction_statuses[0].queryable


async def test_point_specific_invalid_response_skips_only_that_points_remaining_directions(
    hass: HomeAssistant,
) -> None:
    point_a = _point(point_id="PRIVATE-POINT-A", direction=ReadingDirection.UNKNOWN)
    point_b = _point(point_id="PRIVATE-POINT-B", direction=ReadingDirection.IMPORT)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point_b, point_a),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    router_a = AsyncMock()
    router_a.async_get_readings.side_effect = OejpInvalidResponseError("malformed")
    router_b = AsyncMock()
    router_b.async_get_readings.side_effect = (
        OejpAuthorizationError(()),
        _direction_result(ReadingDirection.IMPORT, point=point_b),
    )
    _install_state(coordinator, point_a, router=router_a)
    _install_state(coordinator, point_b, router=router_b)

    data = await coordinator._async_update_data()

    router_a.async_get_readings.assert_awaited_once()
    assert router_b.async_get_readings.await_count == 2
    statuses = {
        (status.supply_point_identity, status.direction): status
        for status in data.direction_statuses
    }
    point_a_identity = stable_supply_point_identity(SECRET, ACCOUNT_ID, point_a.id)
    assert (
        statuses[(point_a_identity, ReadingDirection.EXPORT)].error_class
        is DirectionErrorClass.INVALID_RESPONSE
    )
    assert (
        statuses[(point_a_identity, ReadingDirection.IMPORT)].error_class
        is DirectionErrorClass.INVALID_RESPONSE
    )


async def test_point_failure_invalidates_an_earlier_direction_success(
    hass: HomeAssistant,
) -> None:
    point = _point(direction=ReadingDirection.UNKNOWN)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    router = AsyncMock()
    router.async_get_readings.side_effect = (
        _direction_result(ReadingDirection.EXPORT),
        OejpInvalidResponseError("malformed"),
    )
    _install_state(coordinator, point, router=router)

    data = await coordinator._async_update_data()

    assert {status.error_class for status in data.direction_statuses} == {
        DirectionErrorClass.INVALID_RESPONSE
    }
    assert not any(status.queryable for status in data.direction_statuses)
    assert entity_directions(data, SECRET, ACCOUNT_ID, SUPPLY_POINT_ID) == ()


async def test_regular_poll_orders_points_and_directions_deterministically(
    hass: HomeAssistant,
) -> None:
    point_a = _point(point_id="PRIVATE-POINT-A", direction=ReadingDirection.UNKNOWN)
    point_b = _point(point_id="PRIVATE-POINT-B", direction=ReadingDirection.UNKNOWN)
    coordinator = _coordinator(
        hass,
        accounts=(_account(point_b, point_a),),
        capabilities=_capabilities(
            Capability.IMPORT_READINGS,
            Capability.EXPORT_READINGS,
        ),
    )
    observed: list[tuple[str, ReadingDirection]] = []

    def result_for(
        point: OejpSupplyPoint,
        direction: ReadingDirection,
        _start_at: datetime,
        _end_at: datetime,
    ) -> DirectionReadingResult:
        observed.append((point.id, direction))
        return _direction_result(direction, point=point)

    router_a = AsyncMock()
    router_a.async_get_readings.side_effect = result_for
    router_b = AsyncMock()
    router_b.async_get_readings.side_effect = result_for
    _install_state(coordinator, point_a, router=router_a)
    _install_state(coordinator, point_b, router=router_b)

    await coordinator._async_update_data()

    assert observed == [
        (point_a.id, ReadingDirection.EXPORT),
        (point_a.id, ReadingDirection.IMPORT),
        (point_b.id, ReadingDirection.EXPORT),
        (point_b.id, ReadingDirection.IMPORT),
    ]


async def test_prepare_initializes_each_enabled_supply_point_once(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    backend = Mock()
    checkpoint_backend = Mock()
    checkpoint_backend.async_load = AsyncMock(return_value=None)
    checkpoint_backend.async_save = AsyncMock()
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
        patch(
            "custom_components.octopus_energy_japan.coordinator.HomeAssistantSyncCheckpointBackend",
            return_value=checkpoint_backend,
        ),
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


async def test_prepare_flushes_backend_when_ledger_initialization_fails(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    backend = AsyncMock()
    ledger = Mock()
    ledger.async_initialize = AsyncMock(side_effect=ValueError("corrupt"))
    with (
        patch(
            "custom_components.octopus_energy_japan.coordinator.HomeAssistantLedgerBackend",
            return_value=backend,
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.PersistentIntervalLedger",
            return_value=ledger,
        ),
        pytest.raises(ValueError, match="corrupt"),
    ):
        await coordinator._async_prepare_enabled_supply_points(NOW)

    backend.async_flush.assert_awaited_once_with()
    assert coordinator._supply_points == {}


async def test_shutdown_attempts_every_ledger_flush_after_one_failure(
    hass: HomeAssistant,
) -> None:
    point_a = _point(point_id="PRIVATE-POINT-A")
    point_b = _point(point_id="PRIVATE-POINT-B")
    coordinator = _coordinator(hass, accounts=(_account(point_a, point_b),))
    backend_a = AsyncMock()
    backend_a.async_flush.side_effect = RuntimeError("failed")
    backend_b = AsyncMock()
    coordinator._supply_points = {
        (ACCOUNT_ID, point_a.id): _SupplyPointRuntime(
            point_a,
            backend_a,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            SyncCheckpoint.empty(NOW),
        ),
        (ACCOUNT_ID, point_b.id): _SupplyPointRuntime(
            point_b,
            backend_b,
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            SyncCheckpoint.empty(NOW),
        ),
    }

    await coordinator.async_shutdown_runtime()

    backend_a.async_flush.assert_awaited_once_with()
    backend_b.async_flush.assert_awaited_once_with()


async def test_background_worker_persists_ledger_before_checkpoint_and_publishes(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    direction = ReadingDirection.IMPORT
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(
        BackgroundSyncReason.INITIAL_CURRENT_MONTH,
        "initial-current:test",
    )
    generation = PlannedGeneration(obligation, window.end_at, (window,))
    checkpoint = SyncCheckpoint.empty(NOW).register(generation)
    events: list[str] = []
    ledger = AsyncMock()
    ledger.corrupt_partitions = frozenset()
    ledger.async_records.side_effect = ((), ())
    ledger.async_reconcile.side_effect = lambda *_args: (
        events.append("reconcile") or CorrectionResult()
    )
    backend = AsyncMock()
    backend.async_flush.side_effect = lambda: events.append("ledger_flush")
    checkpoint_backend = AsyncMock()
    checkpoint_backend.async_save.side_effect = lambda _payload: events.append("checkpoint_save")
    router = AsyncMock()
    router.async_get_readings.return_value = _direction_result(direction)
    state = _SupplyPointRuntime(
        point,
        backend,
        ledger,
        router,
        checkpoint_backend,
        checkpoint,
    )
    coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)] = state
    coordinator._ensure_direction_status(state, direction)
    coordinator._record_direction_success(
        state,
        direction,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            direction,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)
    coordinator.async_set_updated_data = Mock()

    await coordinator._async_background_worker()

    assert events == ["reconcile", "ledger_flush", "checkpoint_save"]
    assert state.checkpoint.is_completed(direction, obligation, window)
    assert state.checkpoint.coverage_for(direction) == (
        CoverageWindow(window.start_at, window.end_at),
    )
    coordinator.async_set_updated_data.assert_called_once()


async def test_background_worker_requeues_failed_item_without_spinning(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.side_effect = OejpTransportError("offline")
    _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    coordinator._background_queue.enqueue_item(item)
    retry_waiting = asyncio.Event()

    async def _wait_for_shutdown(_delay: timedelta) -> None:
        retry_waiting.set()
        await asyncio.Event().wait()

    coordinator._async_wait = _wait_for_shutdown  # type: ignore[method-assign]

    task = asyncio.create_task(coordinator._async_background_worker())
    coordinator._background_task = task
    await retry_waiting.wait()
    await coordinator.async_prepare_shutdown()

    router.async_get_readings.assert_awaited_once()
    assert coordinator._background_queue.snapshot() == (item,)


async def test_background_permanent_failure_is_checkpointed_without_retry(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.side_effect = OejpAuthorizationError(())
    _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)
    coordinator.async_set_updated_data = Mock()

    await coordinator._async_background_worker()

    assert coordinator._background_queue.snapshot() == ()
    assert state.checkpoint.is_failed(ReadingDirection.IMPORT, obligation, window)
    state.checkpoint_backend.async_save.assert_awaited_once()
    status = coordinator._direction_statuses[(ACCOUNT_ID, SUPPLY_POINT_ID, ReadingDirection.IMPORT)]
    assert status.error_class is DirectionErrorClass.AUTHORIZATION
    assert not status.queryable


@pytest.mark.parametrize(
    ("error", "expected_class"),
    [
        (OejpNonRetryableHttpError(400), DirectionErrorClass.NON_RETRYABLE_HTTP),
        (OejpQueryValidationError(()), DirectionErrorClass.VALIDATION),
        (OejpNotFoundError(()), DirectionErrorClass.NOT_FOUND),
        (OejpInvalidResponseError("invalid"), DirectionErrorClass.INVALID_RESPONSE),
        (ValueError("invalid"), DirectionErrorClass.INVALID_RESPONSE),
        (LedgerError("ledger"), DirectionErrorClass.LEDGER),
        (OejpError("unknown"), DirectionErrorClass.UNAVAILABLE),
    ],
)
async def test_background_permanent_error_categories_do_not_retry(
    hass: HomeAssistant,
    error: Exception,
    expected_class: DirectionErrorClass,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.side_effect = error
    _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    coordinator._background_queue.enqueue(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        obligation,
    )
    coordinator.async_set_updated_data = Mock()

    await coordinator._async_background_worker()

    status = coordinator._direction_statuses[(ACCOUNT_ID, SUPPLY_POINT_ID, ReadingDirection.IMPORT)]
    assert status.error_class is expected_class
    assert coordinator._background_queue.snapshot() == ()


@pytest.mark.parametrize(
    "error",
    [
        OejpRateLimitError((), retry_after=timedelta(minutes=5)),
        OejpTimeoutError("timeout"),
        OejpTransientHttpError(503, retry_after=timedelta(minutes=2)),
    ],
)
async def test_background_retryable_error_categories_are_deferred(
    hass: HomeAssistant,
    error: Exception,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.side_effect = error
    _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)
    retry_waiting = asyncio.Event()

    async def _wait_for_shutdown(_delay: timedelta) -> None:
        retry_waiting.set()
        await asyncio.Event().wait()

    coordinator._async_wait = _wait_for_shutdown  # type: ignore[method-assign]
    task = asyncio.create_task(coordinator._async_background_worker())
    coordinator._background_task = task
    await retry_waiting.wait()
    await coordinator.async_prepare_shutdown()

    assert coordinator._background_queue.snapshot() == (item,)
    if isinstance(error, OejpRateLimitError):
        assert coordinator._retry.entry_not_before == NOW + timedelta(minutes=5)


async def test_background_authentication_starts_reauth_and_stops_worker(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.side_effect = OejpAuthenticationError(())
    _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)
    with patch.object(coordinator._entry, "async_start_reauth", Mock()) as reauth:
        await coordinator._async_background_worker()

    reauth.assert_called_once_with(hass)
    assert coordinator._background_queue.snapshot() == (item,)
    assert coordinator._reauth_pending
    coordinator._background_started = True
    coordinator._ensure_background_worker()
    assert coordinator._background_task is None


async def test_background_checkpoint_io_failure_requeues_without_advancing(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.return_value = _direction_result(ReadingDirection.IMPORT)
    ledger, _backend = _install_state(coordinator, point, router=router)
    ledger.async_records.side_effect = ((), ())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    original_checkpoint = state.checkpoint
    state.checkpoint_backend.async_save.side_effect = OSError("disk unavailable")
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)
    retry_waiting = asyncio.Event()

    async def _wait_for_shutdown(_delay: timedelta) -> None:
        retry_waiting.set()
        await asyncio.Event().wait()

    coordinator._async_wait = _wait_for_shutdown  # type: ignore[method-assign]
    task = asyncio.create_task(coordinator._async_background_worker())
    coordinator._background_task = task
    await retry_waiting.wait()
    await coordinator.async_prepare_shutdown()

    assert state.checkpoint == original_checkpoint
    assert coordinator._background_queue.snapshot() == (item,)


async def test_pending_poll_preempts_next_background_request(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    router = AsyncMock()
    router.async_get_readings.return_value = _direction_result(ReadingDirection.IMPORT)
    ledger, _backend = _install_state(coordinator, point, router=router)
    ledger.async_records.side_effect = ((), ())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    coordinator._ensure_direction_status(state, ReadingDirection.IMPORT)
    coordinator._record_direction_success(
        state,
        ReadingDirection.IMPORT,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )
    coordinator._background_queue.enqueue(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        obligation,
    )
    coordinator.async_set_updated_data = Mock()
    original_available = coordinator._retry.available
    first_selection = True
    poll_started = asyncio.Event()

    def _start_poll_race(*args: object) -> object:
        nonlocal first_selection
        result = original_available(*args)  # type: ignore[arg-type]
        if first_selection:
            first_selection = False
            coordinator._poll_pending = True
            coordinator._poll_idle.clear()
            poll_started.set()
        return result

    with patch.object(coordinator._retry, "available", side_effect=_start_poll_race):
        task = asyncio.create_task(coordinator._async_background_worker())
        await poll_started.wait()
        router.async_get_readings.assert_not_awaited()
        coordinator._poll_pending = False
        coordinator._poll_idle.set()
        await task

    router.async_get_readings.assert_awaited_once()


async def test_background_sync_starts_only_when_explicitly_enabled(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    coordinator._background_queue.enqueue(
        BackgroundSyncScope(
            "supply-point-" + "f" * 64,
            ReadingDirection.IMPORT,
            window,
        ),
        obligation,
    )

    assert coordinator._background_task is None
    await coordinator.async_start_background_sync()
    assert coordinator._background_task is not None
    await coordinator._background_task
    await coordinator.async_start_background_sync()

    assert coordinator._background_queue.snapshot() == ()


async def test_background_start_resets_state_when_planning_fails(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._async_schedule_background_work = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("checkpoint unavailable")
    )

    with pytest.raises(OSError, match="checkpoint unavailable"):
        await coordinator.async_start_background_sync()

    assert not coordinator._background_started


async def test_background_startup_stagger_applies_once(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._startup_delay = timedelta(minutes=3)
    with patch(
        "custom_components.octopus_energy_japan.coordinator.asyncio.sleep",
        AsyncMock(),
    ) as sleep:
        await coordinator._async_background_worker()
        await coordinator._async_background_worker()

    sleep.assert_awaited_once_with(180.0)


async def test_shutdown_cancels_inflight_background_fetch_and_requeues(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    started = asyncio.Event()

    async def wait_forever(*_args: object) -> DirectionReadingResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    router = AsyncMock()
    router.async_get_readings.side_effect = wait_forever
    _ledger, backend = _install_state(coordinator, point, router=router)
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    item = BackgroundSyncItem(
        BackgroundSyncScope(
            stable_supply_point_identity(SECRET, ACCOUNT_ID, SUPPLY_POINT_ID),
            ReadingDirection.IMPORT,
            window,
        ),
        frozenset({obligation}),
    )
    coordinator._background_queue.enqueue_item(item)

    await coordinator.async_start_background_sync()
    await started.wait()
    await coordinator.async_shutdown_runtime()

    assert coordinator._background_queue.snapshot() == (item,)
    assert coordinator._background_task is None
    backend.async_flush.assert_awaited_once_with()


async def test_prepare_shutdown_waits_for_atomic_section_and_is_idempotent(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def atomic_section() -> None:
        async with coordinator._mutation_lock:
            entered.set()
            await release.wait()

    mutation = asyncio.create_task(atomic_section())
    await entered.wait()
    shutdown = asyncio.create_task(coordinator.async_prepare_shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    release.set()
    await mutation
    await shutdown
    await coordinator.async_prepare_shutdown()

    assert coordinator._closing
    assert coordinator._background_task is None


async def test_resume_runtime_restarts_exactly_one_worker(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    point = _point()
    _install_state(coordinator, point, router=AsyncMock())
    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    window = BackgroundWindow(NOW - timedelta(days=7), NOW - timedelta(hours=72))
    obligation = SyncObligation(BackgroundSyncReason.INITIAL_CURRENT_MONTH, "initial:test")
    state.checkpoint = state.checkpoint.register(
        PlannedGeneration(obligation, window.end_at, (window,))
    )
    coordinator._ensure_direction_status(state, ReadingDirection.IMPORT)
    coordinator._record_direction_success(
        state,
        ReadingDirection.IMPORT,
        coordinator._planner.poll(NOW)[0],
        Mock(observed_at=NOW),
    )
    coordinator._closing = True
    coordinator._background_started = True
    with patch.object(coordinator, "_ensure_background_worker") as ensure:
        await coordinator.async_resume_runtime()
        await coordinator.async_resume_runtime()

    ensure.assert_called_once_with()
    assert not coordinator._closing
    assert len(coordinator._background_queue) == 1


async def test_update_rejects_reauth_and_closing_states(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._reauth_pending = True
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    coordinator._reauth_pending = False
    coordinator._closing = True
    with pytest.raises(UpdateFailed, match="shutting down"):
        await coordinator._async_update_data()


async def test_background_wait_is_interruptible_and_zero_is_immediate(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    await coordinator._async_wait(timedelta(0))

    coordinator._worker_wakeup.set()
    await coordinator._async_wait(timedelta(hours=1))

    coordinator._worker_wakeup.clear()
    waiting = asyncio.create_task(coordinator._async_wait(timedelta(hours=1)))
    await asyncio.sleep(0)
    coordinator._worker_wakeup.set()
    await waiting

    await coordinator._async_wait(timedelta(milliseconds=1))


async def test_prepare_rolls_and_persists_restored_checkpoint(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    backend = AsyncMock()
    ledger = Mock()
    ledger.async_initialize = AsyncMock()
    checkpoint_backend = AsyncMock()
    old = SyncCheckpoint.empty(datetime(2026, 5, 15, tzinfo=UTC))
    checkpoint_backend.async_load.return_value = old.as_dict()
    with (
        patch(
            "custom_components.octopus_energy_japan.coordinator.HomeAssistantLedgerBackend",
            return_value=backend,
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.PersistentIntervalLedger",
            return_value=ledger,
        ),
        patch(
            "custom_components.octopus_energy_japan.coordinator.HomeAssistantSyncCheckpointBackend",
            return_value=checkpoint_backend,
        ),
        patch.object(coordinator, "_reading_router", return_value=Mock()),
    ):
        await coordinator._async_prepare_enabled_supply_points(NOW)

    state = coordinator._supply_points[(ACCOUNT_ID, SUPPLY_POINT_ID)]
    assert state.checkpoint.month_pair_generation == "2026-06_2026-07"
    checkpoint_backend.async_save.assert_awaited_once_with(state.checkpoint.as_dict())


def test_coordinator_exposes_discovery_and_rejects_naive_clock(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    assert coordinator.accounts == (_account(),)
    assert coordinator.capabilities == _capabilities(Capability.IMPORT_READINGS)
    coordinator._now = lambda: datetime(2026, 7, 29)  # noqa: DTZ001

    with pytest.raises(ValueError, match="timezone-aware"):
        coordinator._utc_now()
