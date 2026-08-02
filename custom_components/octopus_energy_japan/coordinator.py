"""Runtime discovery, reading synchronization, and aggregation coordinator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aggregation import (
    TOKYO,
    AggregationSnapshot,
    SupplyPointAggregation,
    aggregate_calendar,
)
from .api import (
    AuthenticatedGraphQLClient,
    CapabilitySnapshot,
    DirectionReadingResult,
    GenericReadingsProvider,
    LegacyHalfHourlyProvider,
    OejpAccount,
    OejpAuthenticationError,
    OejpAuthorizationError,
    OejpError,
    OejpInvalidResponseError,
    OejpNonRetryableHttpError,
    OejpNotFoundError,
    OejpQueryValidationError,
    OejpRateLimitError,
    OejpSupplyPoint,
    OejpTimeoutError,
    OejpTransientHttpError,
    OejpTransportError,
    ReadingDirection,
    ReadingFallbackReason,
    ReadingProviderName,
    ReadingProviderRouter,
    ResourceLifecycle,
    candidate_directions,
)
from .background_sync import (
    BackgroundSyncItem,
    BackgroundSyncPlanner,
    BackgroundSyncQueue,
    BackgroundSyncReason,
    BackgroundSyncScope,
    CoverageWindow,
    SyncCheckpoint,
)
from .const import DOMAIN
from .identity import stable_account_identity, stable_supply_point_identity
from .ledger import (
    CorrectionResult,
    LedgerError,
    LedgerRecord,
    PersistentIntervalLedger,
    expand_authoritative_series,
)
from .ledger_store import HomeAssistantLedgerBackend
from .runtime import selected_historical_resources
from .sync import (
    POLL_INTERVAL,
    SyncReason,
    SyncScheduleState,
    SyncWindow,
    SyncWindowPlanner,
    slow_cadence_due,
    startup_stagger,
)
from .sync_runtime import BackgroundRetryController
from .sync_store import HomeAssistantSyncCheckpointBackend

_LOGGER = logging.getLogger(__name__)

type DiscoveryLoader = Callable[
    [],
    Awaitable[tuple[tuple[OejpAccount, ...], CapabilitySnapshot]],
]
type SupplyPointKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """Safe provider-selection metadata for one supply point."""

    account_identity: str
    supply_point_identity: str
    direction: ReadingDirection
    provider: ReadingProviderName
    fallback_reason: ReadingFallbackReason | None
    observed_at: datetime


class DirectionErrorClass(StrEnum):
    """Privacy-safe current failure category for one reading direction."""

    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"
    NON_RETRYABLE_HTTP = "non_retryable_http"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    LEDGER = "ledger"


@dataclass(frozen=True, slots=True)
class DirectionSyncStatus:
    """Immutable queryability, freshness, and recent coverage state."""

    account_identity: str
    supply_point_identity: str
    direction: ReadingDirection
    queryable: bool = False
    stale: bool = False
    last_success_at: datetime | None = None
    error_class: DirectionErrorClass | None = None
    coverage_start_at: datetime | None = None
    coverage_end_at: datetime | None = None
    background_coverage: tuple[CoverageWindow, ...] = ()


@dataclass(frozen=True, slots=True)
class OejpCoordinatorData:
    """Immutable coordinator snapshot consumed by Home Assistant entities."""

    accounts: tuple[OejpAccount, ...]
    capabilities: CapabilitySnapshot
    aggregation: AggregationSnapshot
    present_supply_points: frozenset[SupplyPointKey]
    enabled_supply_points: frozenset[SupplyPointKey] = frozenset()
    direction_statuses: tuple[DirectionSyncStatus, ...] = ()
    provider_observations: tuple[ProviderObservation, ...] = ()
    correction_count: int = 0
    last_refresh_change_count: int = 0
    corrupt_partition_count: int = 0

    def supply_point_aggregation(
        self,
        account_id: str,
        supply_point_id: str,
        direction: ReadingDirection,
    ) -> SupplyPointAggregation | None:
        """Return one direction projection without exposing it as entity metadata."""
        return next(
            (
                value
                for value in self.aggregation.supply_points
                if value.account_id == account_id
                and value.supply_point_id == supply_point_id
                and value.direction is direction
            ),
            None,
        )

    def supply_point_lifecycle(
        self,
        account_id: str,
        supply_point_id: str,
    ) -> ResourceLifecycle | None:
        """Return the latest discovered lifecycle for one supply point."""
        for account in self.accounts:
            if account.number != account_id:
                continue
            for point in iter_supply_points(account):
                if point.id == supply_point_id:
                    return point.lifecycle
        return None

    def direction_status(
        self,
        account_identity: str,
        supply_point_identity: str,
        direction: ReadingDirection,
    ) -> DirectionSyncStatus | None:
        """Return one privacy-preserving direction status."""
        return next(
            (
                status
                for status in self.direction_statuses
                if status.account_identity == account_identity
                and status.supply_point_identity == supply_point_identity
                and status.direction is direction
            ),
            None,
        )


@dataclass(slots=True)
class _SupplyPointRuntime:
    """Private raw-identifier state for one supply point."""

    supply_point: OejpSupplyPoint
    backend: HomeAssistantLedgerBackend
    ledger: PersistentIntervalLedger
    router: ReadingProviderRouter
    checkpoint_backend: HomeAssistantSyncCheckpointBackend
    checkpoint: SyncCheckpoint


class OejpDataUpdateCoordinator(DataUpdateCoordinator[OejpCoordinatorData]):
    """Synchronize enabled supply points into correction-aware local ledgers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AuthenticatedGraphQLClient,
        accounts: tuple[OejpAccount, ...],
        capabilities: CapabilitySnapshot,
        identity_secret: str,
        discovery_loader: DiscoveryLoader,
        *,
        now: Callable[[], datetime] | None = None,
        startup_delay: timedelta | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=POLL_INTERVAL,
            config_entry=entry,
        )
        self._entry = entry
        self._client = client
        self._accounts = accounts
        self._capabilities = capabilities
        self._identity_secret = identity_secret
        self._discovery_loader = discovery_loader
        self._now = now or (lambda: datetime.now(UTC))
        self._planner = SyncWindowPlanner()
        self._background_planner = BackgroundSyncPlanner()
        self._schedule = SyncScheduleState(last_discovery_at=self._utc_now())
        self._supply_points: dict[SupplyPointKey, _SupplyPointRuntime] = {}
        self._direction_statuses: dict[
            tuple[str, str, ReadingDirection],
            DirectionSyncStatus,
        ] = {}
        self._provider_observations: dict[
            tuple[str, str, ReadingDirection],
            ProviderObservation,
        ] = {}
        self._mutation_lock = asyncio.Lock()
        self._background_queue = BackgroundSyncQueue()
        self._background_task: asyncio.Task[None] | None = None
        self._background_active_scope: BackgroundSyncScope | None = None
        self._background_started = False
        self._closing = False
        self._reauth_pending = False
        self._poll_pending = False
        self._poll_idle = asyncio.Event()
        self._poll_idle.set()
        self._worker_wakeup = asyncio.Event()
        self._retry = BackgroundRetryController()
        self._startup_delay = (
            startup_stagger(entry.entry_id) if startup_delay is None else startup_delay
        )
        self._startup_complete = False

    @property
    def accounts(self) -> tuple[OejpAccount, ...]:
        """Return the most recent discovered accounts."""
        return self._accounts

    @property
    def capabilities(self) -> CapabilitySnapshot:
        """Return the most recent capability snapshot."""
        return self._capabilities

    async def _async_update_data(self) -> OejpCoordinatorData:
        if self._reauth_pending:
            raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed")
        if self._closing:
            raise UpdateFailed("OEJP runtime is shutting down")
        self._poll_pending = True
        self._poll_idle.clear()
        self._worker_wakeup.set()
        now = self._utc_now()
        try:
            await self._async_refresh_discovery_if_due(now)
            await self._async_prepare_enabled_supply_points(now)
            windows = self._planner.poll(now)
            enabled_states = self._enabled_states()
            attempts = tuple(
                (state, direction, window)
                for state in enabled_states
                for direction in candidate_directions(
                    state.supply_point,
                    self._capabilities,
                    previously_queryable=self._previously_queryable_directions(state),
                )
                for window in windows
            )
            corrections: list[CorrectionResult] = []
            successful_directions: set[tuple[str, str, ReadingDirection]] = set()
            point_failures: dict[SupplyPointKey, DirectionErrorClass] = {}
            shared_transient: OejpError | None = None
            for index, (state, direction, window) in enumerate(attempts):
                key = self._direction_key(state, direction)
                self._ensure_direction_status(state, direction)
                point_key = key[:2]
                if point_error := point_failures.get(point_key):
                    self._record_direction_failure(
                        state,
                        direction,
                        point_error,
                        queryable=False,
                    )
                    continue
                try:
                    result, _observation = await self._async_sync_window(
                        state,
                        direction,
                        window,
                    )
                except OejpAuthenticationError:
                    raise
                except (OejpRateLimitError, OejpTransportError) as err:
                    if isinstance(err, OejpNonRetryableHttpError):
                        self._record_direction_failure(
                            state,
                            direction,
                            DirectionErrorClass.NON_RETRYABLE_HTTP,
                            queryable=False,
                        )
                        continue
                    self._record_direction_failure(
                        state,
                        direction,
                        error_class := (
                            DirectionErrorClass.RATE_LIMIT
                            if isinstance(err, OejpRateLimitError)
                            else DirectionErrorClass.TRANSIENT
                        ),
                        queryable=None,
                    )
                    shared_transient = err
                    for pending_state, pending_direction, _pending_window in attempts[index + 1 :]:
                        self._ensure_direction_status(pending_state, pending_direction)
                        self._record_direction_failure(
                            pending_state,
                            pending_direction,
                            error_class,
                            queryable=None,
                        )
                    break
                except OejpAuthorizationError:
                    self._record_direction_failure(
                        state,
                        direction,
                        DirectionErrorClass.AUTHORIZATION,
                        queryable=False,
                    )
                    continue
                except OejpQueryValidationError:
                    self._record_direction_failure(
                        state,
                        direction,
                        DirectionErrorClass.VALIDATION,
                        queryable=False,
                    )
                    continue
                except OejpNotFoundError:
                    point_failures[point_key] = DirectionErrorClass.NOT_FOUND
                    self._record_point_failure(
                        state,
                        DirectionErrorClass.NOT_FOUND,
                    )
                    continue
                except OejpInvalidResponseError:
                    point_failures[point_key] = DirectionErrorClass.INVALID_RESPONSE
                    self._record_point_failure(
                        state,
                        DirectionErrorClass.INVALID_RESPONSE,
                    )
                    continue
                except LedgerError:
                    point_failures[point_key] = DirectionErrorClass.LEDGER
                    self._record_point_failure(
                        state,
                        DirectionErrorClass.LEDGER,
                    )
                    continue
                except ValueError:
                    point_failures[point_key] = DirectionErrorClass.INVALID_RESPONSE
                    self._record_point_failure(
                        state,
                        DirectionErrorClass.INVALID_RESPONSE,
                    )
                    continue
                except OejpError:
                    self._record_direction_failure(
                        state,
                        direction,
                        DirectionErrorClass.UNAVAILABLE,
                        queryable=False,
                    )
                    continue
                else:
                    corrections.append(result)
                    successful_directions.add(key)

            if attempts and not successful_directions and shared_transient is not None:
                raise UpdateFailed("OEJP reading synchronization is temporarily unavailable") from (
                    shared_transient
                )

            await self._async_schedule_background_work(now)
            combined = CorrectionResult.combine(corrections)
            async with self._mutation_lock:
                return await self._async_build_snapshot(
                    now,
                    enabled_states,
                    combined,
                )
        except OejpAuthenticationError as err:
            raise ConfigEntryAuthFailed("OEJP OAuth authorization must be renewed") from err
        except UpdateFailed:
            raise
        except (OejpError, LedgerError, ValueError) as err:
            raise UpdateFailed("OEJP reading synchronization failed") from err
        finally:
            self._poll_pending = False
            self._poll_idle.set()
            self._worker_wakeup.set()

    async def _async_build_snapshot(
        self,
        now: datetime,
        enabled_states: tuple[_SupplyPointRuntime, ...],
        combined: CorrectionResult,
    ) -> OejpCoordinatorData:
        """Build one immutable projection from durable local ledgers."""
        records: list[LedgerRecord] = []
        aggregate_start = _previous_local_month_start(now)
        queryable = {key for key, status in self._direction_statuses.items() if status.queryable}
        enabled_keys = {
            (state.supply_point.account_number, state.supply_point.id) for state in enabled_states
        }
        for state in enabled_states:
            try:
                state_records = await state.ledger.async_records(aggregate_start, now)
            except LedgerError, ValueError:
                for direction in candidate_directions(
                    state.supply_point,
                    self._capabilities,
                    previously_queryable=self._previously_queryable_directions(state),
                ):
                    self._record_direction_failure(
                        state,
                        direction,
                        DirectionErrorClass.LEDGER,
                        queryable=False,
                    )
                continue
            records.extend(
                record
                for record in state_records
                if (
                    record.reading.account_id,
                    record.reading.supply_point_id,
                    record.reading.direction,
                )
                in queryable
            )
        return OejpCoordinatorData(
            accounts=self._accounts,
            capabilities=self._capabilities,
            aggregation=aggregate_calendar(records, now),
            present_supply_points=frozenset(
                (
                    point.account_number,
                    point.id,
                )
                for account in self._accounts
                for point in iter_supply_points(account)
            ),
            enabled_supply_points=frozenset(enabled_keys),
            direction_statuses=tuple(
                self._direction_statuses[key]
                for key in sorted(self._direction_statuses, key=_direction_key_sort)
                if key[:2] in enabled_keys
            ),
            provider_observations=tuple(
                self._provider_observations[key]
                for key in sorted(self._provider_observations, key=_direction_key_sort)
                if key[:2] in enabled_keys
            ),
            correction_count=sum(record.correction_count for record in records),
            last_refresh_change_count=(
                combined.inserted_count + combined.corrected_count + combined.deleted_count
            ),
            corrupt_partition_count=sum(
                len(state.ledger.corrupt_partitions) for state in self._supply_points.values()
            ),
        )

    def async_start_background_sync(self) -> None:
        """Allow queued backfill only after the entry has finished setup."""
        self._background_started = True
        self._ensure_background_worker()

    async def async_prepare_shutdown(self) -> None:
        """Quiesce the worker after any active atomic persistence section."""
        if self._closing and (self._background_task is None or self._background_task.done()):
            return
        self._closing = True
        self._worker_wakeup.set()
        self._poll_idle.set()
        async with self._mutation_lock:
            pass
        task = self._background_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._background_task = None

    async def async_resume_runtime(self) -> None:
        """Reconstruct missing work and resume exactly one worker after unload failure."""
        if not self._closing:
            return
        self._closing = False
        async with self._mutation_lock:
            for state in self._enabled_states():
                for direction in self._previously_queryable_directions(state):
                    for generation in state.checkpoint.generations:
                        state.checkpoint.enqueue_missing(
                            self._background_queue,
                            self._status_identity(state),
                            direction,
                            generation,
                        )
        self._ensure_background_worker()

    async def async_shutdown_runtime(self) -> None:
        """Idempotently stop background work and flush pending durable writes."""
        await self.async_prepare_shutdown()
        for state in self._supply_points.values():
            try:
                await state.backend.async_flush()
            except Exception:
                _LOGGER.exception("Unable to flush an OEJP ledger during runtime cleanup")

    async def _async_refresh_discovery_if_due(self, now: datetime) -> None:
        due = slow_cadence_due(now, self._schedule)
        if not due.discovery:
            return
        accounts, capabilities = await self._discovery_loader()
        self._accounts = accounts
        self._capabilities = capabilities
        self._schedule = SyncScheduleState(
            last_reconciliation_date=self._schedule.last_reconciliation_date,
            last_discovery_at=now,
            last_contract_at=self._schedule.last_contract_at,
            last_billing_at=self._schedule.last_billing_at,
        )
        from .runtime import OejpRuntimeData, async_project_discovered_devices

        runtime = self._entry.runtime_data
        if isinstance(runtime, OejpRuntimeData):
            runtime.accounts = accounts
            runtime.capabilities = capabilities
            async_project_discovered_devices(self.hass, self._entry, runtime)

    async def _async_prepare_enabled_supply_points(self, now: datetime) -> None:
        for point in enabled_supply_points(
            self._entry,
            self._accounts,
            self._identity_secret,
        ):
            key = (point.account_number, point.id)
            state = self._supply_points.get(key)
            if state is None:
                storage_scope = stable_supply_point_identity(
                    self._identity_secret,
                    point.account_number,
                    point.id,
                )
                backend = HomeAssistantLedgerBackend(
                    self.hass,
                    self._entry.entry_id,
                    storage_scope,
                )
                ledger = PersistentIntervalLedger(
                    backend,
                    account_id=point.account_number,
                    supply_point_id=point.id,
                )
                checkpoint_backend = HomeAssistantSyncCheckpointBackend(
                    self.hass,
                    self._entry.entry_id,
                    storage_scope,
                )
                try:
                    await ledger.async_initialize(now)
                    payload = await checkpoint_backend.async_load()
                    checkpoint = (
                        SyncCheckpoint.from_dict(payload)
                        if payload is not None
                        else SyncCheckpoint.empty(now)
                    )
                    rolled = checkpoint.roll_month_pair(now)
                    if rolled != checkpoint:
                        checkpoint = rolled
                        await checkpoint_backend.async_save(checkpoint.as_dict())
                except BaseException:
                    try:
                        await backend.async_flush()
                    except Exception:
                        _LOGGER.exception("Unable to flush a partially initialized OEJP ledger")
                    raise
                state = _SupplyPointRuntime(
                    supply_point=point,
                    backend=backend,
                    ledger=ledger,
                    router=self._reading_router(),
                    checkpoint_backend=checkpoint_backend,
                    checkpoint=checkpoint,
                )
                self._supply_points[key] = state
            else:
                state.supply_point = point
                state.router = self._reading_router()

    async def _async_sync_window(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        window: SyncWindow,
    ) -> tuple[CorrectionResult, ProviderObservation]:
        direction_result = await state.router.async_get_readings(
            state.supply_point,
            direction,
            window.start_at,
            window.end_at,
        )
        observation = self._observation(state, direction, direction_result)
        async with self._mutation_lock:
            result = await self._async_reconcile_result(
                state,
                direction_result,
                window.start_at,
                window.end_at,
            )
            self._provider_observations[self._direction_key(state, direction)] = observation
            self._record_direction_success(state, direction, window, observation)
        return result, observation

    async def _async_reconcile_result(
        self,
        state: _SupplyPointRuntime,
        direction_result: DirectionReadingResult,
        start_at: datetime,
        end_at: datetime,
    ) -> CorrectionResult:
        """Reconcile one provider result while the mutation lock is held."""
        existing = await state.ledger.async_records(start_at, end_at)
        authoritative_series = expand_authoritative_series(
            direction_result.authoritative_series,
            direction_result.authoritative_sources,
            existing,
        )
        return await state.ledger.async_reconcile(
            authoritative_series,
            start_at,
            end_at,
            direction_result.readings,
            direction_result.observed_at,
        )

    async def _async_schedule_background_work(self, now: datetime) -> None:
        """Persist and enqueue initial/daily obligations for queryable directions."""
        async with self._mutation_lock:
            await self._async_schedule_background_work_locked(now)
        self._ensure_background_worker()

    async def _async_schedule_background_work_locked(self, now: datetime) -> None:
        """Mutate checkpoint plans while holding the coordinator lock."""
        due = slow_cadence_due(now, self._schedule)
        daily_plan = self._background_planner.daily(now) if due.reconciliation else None
        for state in self._enabled_states():
            previous_checkpoint = state.checkpoint
            checkpoint = state.checkpoint.roll_month_pair(now)
            if checkpoint.month_pair_generation != previous_checkpoint.month_pair_generation:
                for reason in (
                    BackgroundSyncReason.INITIAL_CURRENT_MONTH,
                    BackgroundSyncReason.INITIAL_PREVIOUS_MONTH,
                ):
                    obsolete = frozenset(
                        generation.obligation.generation
                        for generation in previous_checkpoint.generations
                        if generation.obligation.reason is reason
                    )
                    self._background_queue.remove_obligations(reason, obsolete)
            initial = tuple(
                generation
                for generation in checkpoint.generations
                if generation.obligation.reason
                in {
                    BackgroundSyncReason.INITIAL_CURRENT_MONTH,
                    BackgroundSyncReason.INITIAL_PREVIOUS_MONTH,
                }
            )
            if not initial:
                initial = self._background_planner.initial(now)
                for generation in initial:
                    checkpoint = checkpoint.register(generation)
            if daily_plan is not None:
                existing_daily = next(
                    (
                        generation
                        for generation in checkpoint.generations
                        if generation.obligation.reason is BackgroundSyncReason.DAILY_RECONCILIATION
                        and generation.jst_date == daily_plan.jst_date
                    ),
                    None,
                )
                selected_daily = existing_daily or daily_plan
                checkpoint, obsolete = checkpoint.supersede_daily(selected_daily)
                self._background_queue.remove_obligations(
                    BackgroundSyncReason.DAILY_RECONCILIATION,
                    obsolete,
                )
            if checkpoint != state.checkpoint:
                await state.checkpoint_backend.async_save(checkpoint.as_dict())
                state.checkpoint = checkpoint
            directions = self._previously_queryable_directions(state)
            for direction in directions:
                for generation in state.checkpoint.generations:
                    state.checkpoint.enqueue_missing(
                        self._background_queue,
                        self._status_identity(state),
                        direction,
                        generation,
                    )
            self._apply_checkpoint_coverage(state)
        if daily_plan is not None:
            self._schedule = SyncScheduleState(
                last_reconciliation_date=daily_plan.jst_date,
                last_discovery_at=self._schedule.last_discovery_at,
                last_contract_at=self._schedule.last_contract_at,
                last_billing_at=self._schedule.last_billing_at,
            )
        active_scopes = {item.scope for item in self._background_queue.snapshot()}
        if self._background_active_scope is not None:
            active_scopes.add(self._background_active_scope)
        self._retry.prune(frozenset(active_scopes))

    def _ensure_background_worker(self) -> None:
        if (
            not self._background_started
            or self._closing
            or self._reauth_pending
            or len(self._background_queue) == 0
            or (self._background_task is not None and not self._background_task.done())
        ):
            return
        self._background_task = self.hass.async_create_background_task(
            self._async_background_worker(),
            f"{DOMAIN} background synchronization",
            eager_start=True,
        )

    async def _async_background_worker(self) -> None:
        """Drain durable work with poll priority and bounded retry scheduling."""
        if not self._startup_complete:
            await asyncio.sleep(max(0.0, self._startup_delay.total_seconds()))
            self._startup_complete = True
        while not self._closing:
            await self._poll_idle.wait()
            if self._closing:
                return
            # Clear before inspecting the queue so a poll completion or newly
            # scheduled item cannot be lost between selection and waiting.
            self._worker_wakeup.clear()
            now = self._utc_now()
            async with self._mutation_lock:
                available = self._retry.available(self._background_queue.snapshot(), now)
                item = available.item
                if item is not None:
                    self._background_queue.discard(item.scope)
            if item is None:
                if available.not_before is None:
                    return
                await self._async_wait(available.not_before - now)
                continue
            self._background_active_scope = item.scope
            if self._poll_pending:
                self._background_queue.enqueue_item(item)
                self._background_active_scope = None
                await self._poll_idle.wait()
                continue
            state = self._state_for_identity(item.scope.supply_point_identity)
            if state is None:
                self._retry.resolve(item.scope)
                self._background_active_scope = None
                continue
            try:
                direction_result = await state.router.async_get_readings(
                    state.supply_point,
                    item.scope.direction,
                    item.scope.window.start_at,
                    item.scope.window.end_at,
                )
                async with self._mutation_lock:
                    correction = await self._async_reconcile_result(
                        state,
                        direction_result,
                        item.scope.window.start_at,
                        item.scope.window.end_at,
                    )
                    await state.backend.async_flush()
                    checkpoint = state.checkpoint.mark_durable(item)
                    await state.checkpoint_backend.async_save(checkpoint.as_dict())
                    state.checkpoint = checkpoint
                    observation = self._observation(state, item.scope.direction, direction_result)
                    self._provider_observations[
                        self._direction_key(state, item.scope.direction)
                    ] = observation
                    self._record_direction_success(
                        state,
                        item.scope.direction,
                        SyncWindow(
                            item.scope.window.start_at,
                            item.scope.window.end_at,
                            SyncReason.INITIAL,
                        ),
                        observation,
                    )
                    self._apply_checkpoint_coverage(state)
                    snapshot = await self._async_build_snapshot(
                        self._utc_now(),
                        self._enabled_states(),
                        correction,
                    )
                self.async_set_updated_data(snapshot)
            except asyncio.CancelledError:
                self._background_queue.enqueue_item(item)
                raise
            except OejpAuthenticationError:
                self._background_queue.enqueue_item(item)
                self._reauth_pending = True
                self._entry.async_start_reauth(self.hass)
                return
            except (OejpRateLimitError, OejpTimeoutError, OejpTransientHttpError) as err:
                self._background_queue.enqueue_item(item)
                retry_after = (
                    err.retry_after
                    if isinstance(err, (OejpRateLimitError, OejpTransientHttpError))
                    else None
                )
                self._retry.record_transient(
                    item.scope,
                    self._utc_now(),
                    retry_after=retry_after,
                    rate_limited=isinstance(err, OejpRateLimitError),
                )
            except OejpTransportError as err:
                if isinstance(err, OejpNonRetryableHttpError):
                    await self._async_resolve_permanent_failure(
                        state,
                        item,
                        DirectionErrorClass.NON_RETRYABLE_HTTP,
                    )
                    continue
                self._background_queue.enqueue_item(item)
                self._retry.record_transient(
                    item.scope,
                    self._utc_now(),
                    retry_after=None,
                    rate_limited=False,
                )
            except OSError:
                self._background_queue.enqueue_item(item)
                self._retry.record_transient(
                    item.scope,
                    self._utc_now(),
                    retry_after=None,
                    rate_limited=False,
                )
            except OejpAuthorizationError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.AUTHORIZATION,
                )
            except OejpQueryValidationError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.VALIDATION,
                )
            except OejpNotFoundError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.NOT_FOUND,
                )
            except OejpInvalidResponseError, ValueError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.INVALID_RESPONSE,
                )
            except LedgerError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.LEDGER,
                )
            except OejpError:
                await self._async_resolve_permanent_failure(
                    state,
                    item,
                    DirectionErrorClass.UNAVAILABLE,
                )
            else:
                self._retry.resolve(item.scope)
            finally:
                self._background_active_scope = None

    async def _async_resolve_permanent_failure(
        self,
        state: _SupplyPointRuntime,
        item: BackgroundSyncItem,
        error_class: DirectionErrorClass,
    ) -> None:
        """Persist a generation-scoped failure and publish without retry spin."""
        async with self._mutation_lock:
            checkpoint = state.checkpoint.mark_failed(item, error_class.value)
            await state.checkpoint_backend.async_save(checkpoint.as_dict())
            state.checkpoint = checkpoint
            self._record_direction_failure(
                state,
                item.scope.direction,
                error_class,
                queryable=False,
            )
            snapshot = await self._async_build_snapshot(
                self._utc_now(),
                self._enabled_states(),
                CorrectionResult(),
            )
        self._retry.resolve(item.scope)
        self.async_set_updated_data(snapshot)

    async def _async_wait(self, delay: timedelta) -> None:
        """Wait interruptibly without holding the request gate or mutation lock."""
        seconds = max(0.0, delay.total_seconds())
        if seconds == 0:
            return
        try:
            async with asyncio.timeout(seconds):
                await self._worker_wakeup.wait()
        except TimeoutError:
            pass

    def _state_for_identity(self, identity: str) -> _SupplyPointRuntime | None:
        return next(
            (
                state
                for state in self._supply_points.values()
                if self._status_identity(state) == identity
            ),
            None,
        )

    def _status_identity(self, state: _SupplyPointRuntime) -> str:
        return stable_supply_point_identity(
            self._identity_secret,
            state.supply_point.account_number,
            state.supply_point.id,
        )

    def _apply_checkpoint_coverage(self, state: _SupplyPointRuntime) -> None:
        for direction in self._previously_queryable_directions(state):
            key = self._direction_key(state, direction)
            previous = self._direction_statuses[key]
            self._direction_statuses[key] = DirectionSyncStatus(
                account_identity=previous.account_identity,
                supply_point_identity=previous.supply_point_identity,
                direction=direction,
                queryable=previous.queryable,
                stale=previous.stale,
                last_success_at=previous.last_success_at,
                error_class=previous.error_class,
                coverage_start_at=previous.coverage_start_at,
                coverage_end_at=previous.coverage_end_at,
                background_coverage=state.checkpoint.coverage_for(direction),
            )

    def _observation(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        result: DirectionReadingResult,
    ) -> ProviderObservation:
        return ProviderObservation(
            account_identity=stable_account_identity(
                self._identity_secret, state.supply_point.account_number
            ),
            supply_point_identity=self._status_identity(state),
            direction=direction,
            provider=result.provider,
            fallback_reason=result.fallback_reason,
            observed_at=result.observed_at,
        )

    def _reading_router(self) -> ReadingProviderRouter:
        return ReadingProviderRouter(
            GenericReadingsProvider(
                self._client,
                self._capabilities,
                now=self._now,
            ),
            LegacyHalfHourlyProvider(
                self._client,
                self._capabilities,
                now=self._now,
            ),
            self._capabilities,
            now=self._now,
        )

    def _enabled_states(self) -> tuple[_SupplyPointRuntime, ...]:
        enabled = {
            (point.account_number, point.id)
            for point in enabled_supply_points(
                self._entry,
                self._accounts,
                self._identity_secret,
            )
        }
        return tuple(
            self._supply_points[key] for key in sorted(enabled) if key in self._supply_points
        )

    def _previously_queryable_directions(
        self,
        state: _SupplyPointRuntime,
    ) -> tuple[ReadingDirection, ...]:
        return tuple(
            sorted(
                (
                    direction
                    for (
                        account_id,
                        point_id,
                        direction,
                    ), status in self._direction_statuses.items()
                    if account_id == state.supply_point.account_number
                    and point_id == state.supply_point.id
                    and status.queryable
                ),
                key=lambda direction: direction.value,
            )
        )

    def _direction_key(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
    ) -> tuple[str, str, ReadingDirection]:
        return (
            state.supply_point.account_number,
            state.supply_point.id,
            direction,
        )

    def _ensure_direction_status(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
    ) -> DirectionSyncStatus:
        key = self._direction_key(state, direction)
        status = self._direction_statuses.get(key)
        if status is None:
            status = DirectionSyncStatus(
                account_identity=stable_account_identity(
                    self._identity_secret,
                    state.supply_point.account_number,
                ),
                supply_point_identity=stable_supply_point_identity(
                    self._identity_secret,
                    state.supply_point.account_number,
                    state.supply_point.id,
                ),
                direction=direction,
            )
            self._direction_statuses[key] = status
        return status

    def _record_direction_success(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        window: SyncWindow,
        observation: ProviderObservation,
    ) -> None:
        key = self._direction_key(state, direction)
        previous = self._ensure_direction_status(state, direction)
        self._direction_statuses[key] = DirectionSyncStatus(
            account_identity=previous.account_identity,
            supply_point_identity=previous.supply_point_identity,
            direction=direction,
            queryable=True,
            stale=False,
            last_success_at=observation.observed_at,
            error_class=None,
            coverage_start_at=(
                min(previous.coverage_start_at, window.start_at)
                if previous.coverage_start_at is not None
                else window.start_at
            ),
            coverage_end_at=(
                max(previous.coverage_end_at, window.end_at)
                if previous.coverage_end_at is not None
                else window.end_at
            ),
            background_coverage=previous.background_coverage,
        )

    def _record_direction_failure(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        error_class: DirectionErrorClass,
        *,
        queryable: bool | None,
    ) -> None:
        key = self._direction_key(state, direction)
        previous = self._ensure_direction_status(state, direction)
        self._direction_statuses[key] = DirectionSyncStatus(
            account_identity=previous.account_identity,
            supply_point_identity=previous.supply_point_identity,
            direction=direction,
            queryable=(previous.queryable if queryable is None else queryable),
            stale=(queryable is None or previous.last_success_at is not None),
            last_success_at=previous.last_success_at,
            error_class=error_class,
            coverage_start_at=previous.coverage_start_at,
            coverage_end_at=previous.coverage_end_at,
            background_coverage=previous.background_coverage,
        )

    def _record_point_failure(
        self,
        state: _SupplyPointRuntime,
        error_class: DirectionErrorClass,
    ) -> None:
        directions = set(
            candidate_directions(
                state.supply_point,
                self._capabilities,
                previously_queryable=self._previously_queryable_directions(state),
            )
        )
        directions.update(
            direction
            for account_id, point_id, direction in self._direction_statuses
            if account_id == state.supply_point.account_number and point_id == state.supply_point.id
        )
        for direction in sorted(directions, key=lambda value: value.value):
            self._record_direction_failure(
                state,
                direction,
                error_class,
                queryable=False,
            )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Coordinator clock must be timezone-aware")
        return value.astimezone(UTC)


def iter_supply_points(account: OejpAccount) -> tuple[OejpSupplyPoint, ...]:
    """Return every supply point without selecting the first account/property."""
    return tuple(point for property_ in account.properties for point in property_.supply_points)


def enabled_supply_points(
    entry: ConfigEntry,
    accounts: Iterable[OejpAccount],
    identity_secret: str,
) -> tuple[OejpSupplyPoint, ...]:
    """Select active/unknown resources and explicitly enabled history."""
    selected = selected_historical_resources(entry)
    enabled: list[OejpSupplyPoint] = []
    for account in accounts:
        account_identity = stable_account_identity(identity_secret, account.number)
        account_enabled = (
            account.lifecycle is not ResourceLifecycle.HISTORICAL or account_identity in selected
        )
        if not account_enabled:
            continue
        for point in iter_supply_points(account):
            point_identity = stable_supply_point_identity(
                identity_secret,
                account.number,
                point.id,
            )
            if point.lifecycle is not ResourceLifecycle.HISTORICAL or point_identity in selected:
                enabled.append(point)
    return tuple(
        sorted(
            enabled,
            key=lambda point: (point.account_number, point.id),
        )
    )


def entity_directions(
    data: OejpCoordinatorData | None,
    identity_secret: str,
    account_id: str,
    supply_point_id: str,
) -> tuple[ReadingDirection, ...]:
    """Return only directions proven queryable by an authoritative success."""
    if data is None:
        return ()
    account_identity = stable_account_identity(identity_secret, account_id)
    point_identity = stable_supply_point_identity(
        identity_secret,
        account_id,
        supply_point_id,
    )
    return tuple(
        sorted(
            {
                status.direction
                for status in data.direction_statuses
                if status.account_identity == account_identity
                and status.supply_point_identity == point_identity
                and status.queryable
            },
            key=lambda direction: direction.value,
        )
    )


def _direction_key_sort(
    key: tuple[str, str, ReadingDirection],
) -> tuple[str, str, str]:
    return key[0], key[1], key[2].value


def _previous_local_month_start(now: datetime) -> datetime:
    local_month = now.astimezone(TOKYO).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_month.month == 1:
        previous = local_month.replace(
            year=local_month.year - 1,
            month=12,
        )
    else:
        previous = local_month.replace(month=local_month.month - 1)
    return previous.astimezone(UTC)
