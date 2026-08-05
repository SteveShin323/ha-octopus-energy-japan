"""Runtime discovery, reading synchronization, and aggregation coordinator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aggregation import (
    TOKYO,
    AggregationSnapshot,
    SupplyPointAggregation,
    aggregate_calendar,
    apply_calendar_coverage,
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
    LedgerMergeStatus,
    LedgerRecord,
    PersistentIntervalLedger,
    expand_authoritative_series,
)
from .ledger_store import HomeAssistantLedgerBackend
from .runtime import selected_historical_resources
from .statistics_runtime import StatisticsProjector
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


class _FailureScope(StrEnum):
    """Whether a failure condemns one direction or the whole supply point."""

    DIRECTION = "direction"
    POINT = "point"


@dataclass(frozen=True, slots=True)
class _TriageRule:
    """How one exception class is recorded when a reading attempt fails."""

    exception: type[BaseException]
    error_class: DirectionErrorClass
    scope: _FailureScope
    # `None` leaves queryability as it was, which also marks the direction stale. `False`
    # says the provider answered and refused, so the direction is not worth asking again
    # this poll.
    queryable: bool | None
    # A shared fault: the remaining attempts would fail the same way, so they are recorded
    # without being tried and the poll ends.
    interrupts_poll: bool = False


# Most specific first, because `isinstance` takes the first match. Two orderings are
# load-bearing: `OejpNonRetryableHttpError` is an `OejpTransportError` but must not be
# treated as transient, and `OejpError` is the catch-all so nothing may follow it.
# `test_coordinator.py` asserts the ordering rather than leaving it to source order.
#
# `OejpAuthenticationError` is deliberately absent. It is re-raised before this table is
# consulted, because reauthentication is Home Assistant's own flow and not a direction
# failure — and it would otherwise be caught by the `OejpError` entry.
_TRIAGE_RULES: Final = (
    _TriageRule(
        OejpNonRetryableHttpError,
        DirectionErrorClass.NON_RETRYABLE_HTTP,
        _FailureScope.DIRECTION,
        queryable=False,
    ),
    _TriageRule(
        OejpRateLimitError,
        DirectionErrorClass.RATE_LIMIT,
        _FailureScope.DIRECTION,
        queryable=None,
        interrupts_poll=True,
    ),
    _TriageRule(
        OejpTransportError,
        DirectionErrorClass.TRANSIENT,
        _FailureScope.DIRECTION,
        queryable=None,
        interrupts_poll=True,
    ),
    _TriageRule(
        OejpAuthorizationError,
        DirectionErrorClass.AUTHORIZATION,
        _FailureScope.DIRECTION,
        queryable=False,
    ),
    _TriageRule(
        OejpQueryValidationError,
        DirectionErrorClass.VALIDATION,
        _FailureScope.DIRECTION,
        queryable=False,
    ),
    _TriageRule(
        OejpNotFoundError,
        DirectionErrorClass.NOT_FOUND,
        _FailureScope.POINT,
        queryable=False,
    ),
    _TriageRule(
        OejpInvalidResponseError,
        DirectionErrorClass.INVALID_RESPONSE,
        _FailureScope.POINT,
        queryable=False,
    ),
    _TriageRule(
        LedgerError,
        DirectionErrorClass.LEDGER,
        _FailureScope.POINT,
        queryable=False,
    ),
    _TriageRule(
        ValueError,
        DirectionErrorClass.INVALID_RESPONSE,
        _FailureScope.POINT,
        queryable=False,
    ),
    _TriageRule(
        OejpError,
        DirectionErrorClass.UNAVAILABLE,
        _FailureScope.DIRECTION,
        queryable=False,
    ),
)

# The caught set is derived from the table so the two cannot drift apart: an exception the
# table describes is always caught, and one it does not describe is never swallowed.
_TRIAGE_EXCEPTIONS: Final = tuple(rule.exception for rule in _TRIAGE_RULES)


def _triage(error: BaseException) -> _TriageRule:
    """Return how a failed reading attempt is recorded."""
    for rule in _TRIAGE_RULES:
        if isinstance(error, rule.exception):
            return rule
    # Unreachable: the caught set is derived from this table. It guards against someone
    # replacing that derivation with a hand-written tuple.
    raise AssertionError(  # pragma: no cover - _TRIAGE_EXCEPTIONS makes this unreachable
        "A caught exception must be described by the triage table"
    )


class _WorkerDisposition(StrEnum):
    """What the background worker does with a failed item, beyond recording it."""

    # Requeue and let the retry controller decide when to try again.
    RETRY = "retry"
    # Stop retrying: resolve the scope and record the class `_triage` assigns.
    PERMANENT = "permanent"
    # Requeue, hand over to Home Assistant's reauthentication flow, and stop the worker.
    REAUTH = "reauth"


@dataclass(frozen=True, slots=True)
class _WorkerRule:
    """What one exception class means for a background item."""

    exception: type[BaseException]
    disposition: _WorkerDisposition


# Most specific first, same as `_TRIAGE_RULES`, and asserted by the same kind of test. One
# ordering is load-bearing: `OejpNonRetryableHttpError` is an `OejpTransportError`, and
# retrying it forever would be wrong, so it must come first.
#
# `OejpTransportError` covers `OejpTimeoutError` and `OejpTransientHttpError`, which the
# ladder listed separately for identical treatment. Only the retry delay differs, and that is
# read from the exception rather than the table.
#
# Failure *categories* are not repeated here. A permanent failure is recorded with the class
# `_triage` assigns, so the poll and the worker cannot disagree about what an exception means.
_WORKER_RULES: Final = (
    _WorkerRule(OejpAuthenticationError, _WorkerDisposition.REAUTH),
    _WorkerRule(OejpNonRetryableHttpError, _WorkerDisposition.PERMANENT),
    _WorkerRule(OejpRateLimitError, _WorkerDisposition.RETRY),
    _WorkerRule(OejpTransportError, _WorkerDisposition.RETRY),
    _WorkerRule(OSError, _WorkerDisposition.RETRY),
    _WorkerRule(OejpAuthorizationError, _WorkerDisposition.PERMANENT),
    _WorkerRule(OejpQueryValidationError, _WorkerDisposition.PERMANENT),
    _WorkerRule(OejpNotFoundError, _WorkerDisposition.PERMANENT),
    _WorkerRule(OejpInvalidResponseError, _WorkerDisposition.PERMANENT),
    _WorkerRule(ValueError, _WorkerDisposition.PERMANENT),
    _WorkerRule(LedgerError, _WorkerDisposition.PERMANENT),
    _WorkerRule(OejpError, _WorkerDisposition.PERMANENT),
)

# Derived from the table, for the same reason as `_TRIAGE_EXCEPTIONS`.
_WORKER_EXCEPTIONS: Final = tuple(rule.exception for rule in _WORKER_RULES)


def _worker_rule(error: BaseException) -> _WorkerRule:
    """Return what the background worker does with a failed item."""
    for rule in _WORKER_RULES:
        if isinstance(error, rule.exception):
            return rule
    # Unreachable: the caught set is derived from this table.
    raise AssertionError(  # pragma: no cover - _WORKER_EXCEPTIONS makes this unreachable
        "A caught exception must be described by the worker table"
    )


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
    discarded_checkpoint_count: int = 0

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
        point = self._supply_point(account_id, supply_point_id)
        return point.lifecycle if point is not None else None

    def supply_point_reading_day(
        self,
        account_id: str,
        supply_point_id: str,
    ) -> int | None:
        """Return the day of the month one supply point's meter is read on."""
        point = self._supply_point(account_id, supply_point_id)
        return point.reading_day_of_month if point is not None else None

    def supply_point_address(
        self,
        account_id: str,
        supply_point_id: str,
    ) -> str | None:
        """Return the provider's address for the property a supply point belongs to.

        The postcode is appended when the address does not already contain it, because the
        provider returns them separately and either alone is ambiguous for a customer with
        more than one property.
        """
        for account in self.accounts:
            if account.number != account_id:
                continue
            for property_ in account.properties:
                if all(point.id != supply_point_id for point in property_.supply_points):
                    continue
                address = property_.address
                postcode = property_.postcode
                if address is None:
                    return postcode
                if postcode is None or postcode in address:
                    return address
                return f"{postcode} {address}"
        return None

    def _supply_point(self, account_id: str, supply_point_id: str) -> OejpSupplyPoint | None:
        for account in self.accounts:
            if account.number != account_id:
                continue
            for point in iter_supply_points(account):
                if point.id == supply_point_id:
                    return point
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


@dataclass(frozen=True, slots=True)
class _StatisticsPending:
    """Earliest dirty hour and directions requiring a destructive rebuild."""

    dirty_from: datetime | None
    reset_directions: frozenset[ReadingDirection] = frozenset()


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
        statistics_projector: StatisticsProjector | None = None,
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
        self._statistics_projector = statistics_projector
        self._statistics_pending: dict[SupplyPointKey, _StatisticsPending] = {}
        self._statistics_failures: set[SupplyPointKey] = set()
        # Supply points whose stored checkpoint could not be read and was replaced.
        # Surfaced in diagnostics so re-reading old windows has a visible cause.
        self._discarded_checkpoints: set[SupplyPointKey] = set()

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
            raise ConfigEntryAuthFailed(
                "OEJP OAuth authorization must be renewed",
                translation_domain=DOMAIN,
                translation_key="reauth_required",
            )
        if self._closing:
            raise UpdateFailed(
                "OEJP runtime is shutting down",
                translation_domain=DOMAIN,
                translation_key="shutting_down",
            )
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
            # Widened from `OejpError` because the triage table's caught set is typed by
            # the table, which also carries `ValueError`. Only the two interrupting rules
            # assign this, and both are `OejpError`, but stating that here would need a
            # narrowing branch that can never be false.
            shared_transient: BaseException | None = None
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
                    # Reauthentication is Home Assistant's own flow, never a direction
                    # failure. This must precede the table, whose catch-all would take it.
                    raise
                except _TRIAGE_EXCEPTIONS as err:
                    rule = _triage(err)
                    if rule.scope is _FailureScope.POINT:
                        # The supply point answered in a way that condemns every direction
                        # on it, so the remaining ones are not attempted.
                        point_failures[point_key] = rule.error_class
                        self._record_point_failure(state, rule.error_class)
                    else:
                        self._record_direction_failure(
                            state,
                            direction,
                            rule.error_class,
                            queryable=rule.queryable,
                        )
                    if not rule.interrupts_poll:
                        continue
                    shared_transient = err
                    for pending_state, pending_direction, _pending_window in attempts[index + 1 :]:
                        self._ensure_direction_status(pending_state, pending_direction)
                        self._record_direction_failure(
                            pending_state,
                            pending_direction,
                            rule.error_class,
                            queryable=rule.queryable,
                        )
                    break
                else:
                    corrections.append(result)
                    successful_directions.add(key)

            if attempts and not successful_directions and shared_transient is not None:
                raise UpdateFailed(
                    "OEJP reading synchronization is temporarily unavailable",
                    translation_domain=DOMAIN,
                    translation_key="readings_unavailable",
                ) from (shared_transient)

            if self._background_started:
                await self._async_schedule_background_work(now)
            combined = CorrectionResult.combine(corrections)
            async with self._mutation_lock:
                self._mark_statistics_dirty(combined)
                snapshot = await self._async_build_snapshot(
                    now,
                    enabled_states,
                    combined,
                )
                await self._async_publish_pending_statistics(now)
                return snapshot
        except OejpAuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "OEJP OAuth authorization must be renewed",
                translation_domain=DOMAIN,
                translation_key="reauth_required",
            ) from err
        except UpdateFailed:
            raise
        except (OejpError, LedgerError, ValueError) as err:
            raise UpdateFailed(
                "OEJP reading synchronization failed",
                translation_domain=DOMAIN,
                translation_key="readings_failed",
            ) from err
        except OSError as err:
            # Storage, not the network: the config directory momentarily unwritable, or a
            # full disk, reaching this through a checkpoint or ledger write. Home Assistant
            # logs anything that is not `UpdateFailed` as "Unexpected error fetching …" with
            # a traceback, which reads as an integration bug rather than a disk problem. The
            # background worker already treats the same fault as retryable.
            raise UpdateFailed(
                "OEJP local storage is unavailable",
                translation_domain=DOMAIN,
                translation_key="storage_unavailable",
            ) from err
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
        queryable = {key for key, status in self._direction_statuses.items() if status.queryable}
        series = tuple(
            sorted(
                (key for key in queryable if key[:2] in enabled_keys),
                key=_direction_key_sort,
            )
        )
        coverage: dict[
            tuple[str, str, ReadingDirection],
            tuple[tuple[datetime, datetime], ...],
        ] = {}
        for key, status in self._direction_statuses.items():
            if key not in queryable or key[:2] not in enabled_keys:
                continue
            ranges = [(window.start_at, window.end_at) for window in status.background_coverage]
            if status.coverage_start_at is not None and status.coverage_end_at is not None:
                ranges.append((status.coverage_start_at, status.coverage_end_at))
            coverage[key] = tuple(ranges)
        aggregation = apply_calendar_coverage(
            aggregate_calendar(records, now, series=series),
            coverage,
        )
        return OejpCoordinatorData(
            accounts=self._accounts,
            capabilities=self._capabilities,
            aggregation=aggregation,
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
            discarded_checkpoint_count=len(self._discarded_checkpoints),
        )

    async def async_start_background_sync(self) -> None:
        """Plan and start backfill only after the entry has finished setup."""
        if self._background_started:
            return
        self._background_started = True
        try:
            await self._async_schedule_background_work(self._utc_now())
        except BaseException:
            self._background_started = False
            raise

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
        await self._async_apply_resource_lifecycle()
        await self._async_reconsider_failures_after_discovery()
        from .runtime import OejpRuntimeData, async_project_discovered_devices

        runtime = self._entry.runtime_data
        if isinstance(runtime, OejpRuntimeData):
            runtime.accounts = accounts
            runtime.capabilities = capabilities
            async_project_discovered_devices(self.hass, self._entry, runtime)

    async def _async_reconsider_failures_after_discovery(self) -> None:
        """Retry permanent windows only after a new relevant discovery generation."""
        async with self._mutation_lock:
            for state in self._enabled_states():
                checkpoint = state.checkpoint
                for direction in candidate_directions(
                    state.supply_point,
                    self._capabilities,
                    previously_queryable=self._previously_queryable_directions(state),
                ):
                    checkpoint = checkpoint.clear_failures(direction)
                if checkpoint != state.checkpoint:
                    await state.checkpoint_backend.async_save(checkpoint.as_dict())
                    state.checkpoint = checkpoint

    async def _async_apply_resource_lifecycle(self) -> None:
        """Cancel queued work for disabled or missing resources without deleting stores."""
        enabled_identities = frozenset(
            stable_supply_point_identity(
                self._identity_secret,
                point.account_number,
                point.id,
            )
            for point in enabled_supply_points(
                self._entry,
                self._accounts,
                self._identity_secret,
            )
        )
        async with self._mutation_lock:
            self._background_queue.retain_supply_points(enabled_identities)
            active_scopes = {item.scope for item in self._background_queue.snapshot()}
            if self._background_active_scope is not None:
                active_scopes.add(self._background_active_scope)
            self._retry.prune(frozenset(active_scopes))

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
                    checkpoint = self._restore_checkpoint(payload, now, key)
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
                self._statistics_pending[key] = _StatisticsPending(None)
            else:
                state.supply_point = point
                state.router = self._reading_router()

    def _restore_checkpoint(
        self,
        payload: Mapping[str, Any] | None,
        now: datetime,
        key: SupplyPointKey,
    ) -> SyncCheckpoint:
        """Return the stored checkpoint, or a fresh one when it cannot be read.

        A checkpoint records which windows have already been fetched. It is derived from the
        ledger rather than a source of truth, so discarding one costs re-reading those windows
        and loses no data: the ledger is keyed, so a re-fetched interval replaces itself.

        Failing instead is what used to happen. `from_dict` raises `ValueError` on a schema
        version it does not recognise, the poll turns that into `UpdateFailed`, and the entry
        can never synchronise again — so raising the checkpoint's schema version would have
        broken every installation until the user deleted and re-added the entry, losing the
        history that the delete takes with it.

        When a future schema has state worth carrying forward, migrate it inside
        `SyncCheckpoint.from_dict` the way the ledger migrates a partition. This stays as the
        net underneath that.
        """
        if payload is None:
            return SyncCheckpoint.empty(now)
        try:
            return SyncCheckpoint.from_dict(payload)
        except ValueError:
            _LOGGER.warning(
                "Discarding an unreadable OEJP synchronization checkpoint for one supply "
                "point and planning again from the current month. Stored readings are "
                "unaffected; the windows it recorded will be re-read"
            )
            self._discarded_checkpoints.add(key)
            return SyncCheckpoint.empty(now)

    def _mark_statistics_dirty(self, correction: CorrectionResult) -> None:
        """Retain the earliest unprojected ledger change for each supply point."""
        changed_statuses = {
            LedgerMergeStatus.INSERTED,
            LedgerMergeStatus.CORRECTED,
            LedgerMergeStatus.DELETED,
        }
        for change in correction.changes:
            if change.status not in changed_statuses:
                continue
            key = (
                change.key.series.account_id,
                change.key.series.supply_point_id,
            )
            if key not in self._supply_points:
                continue
            reset_direction = (
                change.key.series.direction if change.status is LedgerMergeStatus.DELETED else None
            )
            if key not in self._statistics_pending:
                self._statistics_pending[key] = _StatisticsPending(
                    change.key.start_at,
                    frozenset({reset_direction}) if reset_direction is not None else frozenset(),
                )
                continue
            pending = self._statistics_pending[key]
            dirty_from = (
                None if pending.dirty_from is None else min(pending.dirty_from, change.key.start_at)
            )
            reset_directions = pending.reset_directions
            if reset_direction is not None:
                reset_directions |= {reset_direction}
            self._statistics_pending[key] = _StatisticsPending(
                dirty_from,
                frozenset(reset_directions),
            )

    async def _async_publish_pending_statistics(self, generated_at: datetime) -> None:
        """Publish durable ledger projections without failing consumption updates.

        Called under two different lock disciplines: the poll calls it *without* the mutation
        lock, and the background worker calls it *with* the lock held. The two can overlap,
        because the worker only re-checks `_poll_pending` before its network request and a
        poll can start during that request.

        That is why the marker is popped only when it still equals the one just projected. A
        ledger change marked dirty while a projection was already awaiting would otherwise be
        discarded, leaving those statistics stale until something else dirtied the same supply
        point. `test_coordinator.py` pins both sides of that check.
        """
        if self._statistics_projector is None:
            self._statistics_pending.clear()
            self._statistics_failures.clear()
            return
        for key, pending in tuple(self._statistics_pending.items()):
            state = self._supply_points.get(key)
            if state is None:
                self._statistics_pending.pop(key, None)
                self._statistics_failures.discard(key)
                continue
            try:
                await state.backend.async_flush()
                await self._statistics_projector.async_project_supply_point(
                    state.ledger,
                    key[0],
                    key[1],
                    generated_at,
                    dirty_from=pending.dirty_from,
                    reset_directions=pending.reset_directions,
                )
            except Exception:
                if key not in self._statistics_failures:
                    _LOGGER.exception("Unable to project OEJP Energy Dashboard statistics")
                    self._statistics_failures.add(key)
            else:
                if key in self._statistics_failures:
                    _LOGGER.info("OEJP Energy Dashboard statistics projection recovered")
                    self._statistics_failures.discard(key)
                if self._statistics_pending.get(key) == pending:
                    self._statistics_pending.pop(key, None)

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
            directions = self._previously_queryable_directions(state)
            if daily_plan is not None:
                for direction in directions:
                    checkpoint = checkpoint.clear_failures(direction)
            if checkpoint != state.checkpoint:
                await state.checkpoint_backend.async_save(checkpoint.as_dict())
                state.checkpoint = checkpoint
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
                    self._mark_statistics_dirty(correction)
                    await self._async_publish_pending_statistics(self._utc_now())
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
                # A shutdown, not a failure. The item is requeued so it survives, and the
                # cancellation propagates — swallowing it would keep the worker alive.
                self._background_queue.enqueue_item(item)
                raise
            except _WORKER_EXCEPTIONS as err:
                rule = _worker_rule(err)
                if rule.disposition is _WorkerDisposition.PERMANENT:
                    # Recorded with the class the poll would assign, so the two paths cannot
                    # disagree about what an exception means.
                    await self._async_resolve_permanent_failure(
                        state,
                        item,
                        _triage(err).error_class,
                    )
                    continue
                self._background_queue.enqueue_item(item)
                if rule.disposition is _WorkerDisposition.REAUTH:
                    self._reauth_pending = True
                    self._entry.async_start_reauth(self.hass)
                    return
                self._retry.record_transient(
                    item.scope,
                    self._utc_now(),
                    # Only the provider states a delay; a transport or storage fault leaves
                    # the interval to the retry controller.
                    retry_after=(
                        err.retry_after
                        if isinstance(err, OejpRateLimitError | OejpTransientHttpError)
                        else None
                    ),
                    rate_limited=isinstance(err, OejpRateLimitError),
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
        account_selected = account_identity in selected
        account_enabled = account.lifecycle is not ResourceLifecycle.HISTORICAL or account_selected
        if not account_enabled:
            continue
        for point in iter_supply_points(account):
            point_identity = stable_supply_point_identity(
                identity_secret,
                account.number,
                point.id,
            )
            if (
                account_selected
                or point.lifecycle is not ResourceLifecycle.HISTORICAL
                or point_identity in selected
            ):
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
