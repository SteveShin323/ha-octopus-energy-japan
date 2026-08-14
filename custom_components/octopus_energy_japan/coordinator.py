"""Runtime discovery, reading synchronization, and aggregation coordinator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
    PointsAllowance,
    ReadingDirection,
    ReadingFallbackReason,
    ReadingProviderName,
    ReadingProviderRouter,
    ResourceLifecycle,
    async_fetch_points_allowance,
    candidate_directions,
)
from .background_sync import (
    BACKFILL_GENERATION,
    GAP_REFILL_GENERATION,
    BackfillState,
    BackgroundSyncItem,
    BackgroundSyncPlanner,
    BackgroundSyncQueue,
    BackgroundSyncReason,
    BackgroundSyncScope,
    BackgroundWindow,
    CoverageWindow,
    SyncCheckpoint,
    SyncObligation,
    initial_floor,
)
from .billing_period import BillingPeriodCalendar
from .const import DOMAIN
from .identity import stable_account_identity, stable_supply_point_identity
from .ledger import (
    CorrectionResult,
    IntervalGap,
    LedgerError,
    LedgerMergeStatus,
    LedgerRecord,
    PersistentIntervalLedger,
    expand_authoritative_series,
    interval_gaps,
    partition_bounds,
)
from .ledger_store import HomeAssistantLedgerBackend
from .runtime import selected_historical_resources
from .statistics_runtime import StatisticsProjector
from .sync import (
    BACKFILL_EMPTY_WINDOWS,
    BACKFILL_MAX_HISTORY,
    BACKFILL_MIN_INTERVAL,
    BACKFILL_POINT_RESERVE,
    MAX_QUERY_WINDOW,
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


# How stale a reading of the point allowance may be before the walk asks again. One minute
# costs 5 points against a 50,000 allowance while a walk spends about 340 in the same minute.
_ALLOWANCE_MAX_AGE: Final = timedelta(minutes=1)

# How recent an absence is left alone when looking for holes. The provider publishes a half hour
# somewhere between 4 hours and 4.6 days after it happens, measured over 245 readings on an
# account collecting normally, so anything younger than this has not gone missing — it has not
# been published. Reporting it would make every installation look permanently damaged.
HISTORY_GAP_GRACE: Final = timedelta(days=7)


def _is_backfill(item: BackgroundSyncItem) -> bool:
    """Report whether every obligation on an item is the history walk.

    All, not any: an ordinary window that happens to coincide with one the walk wants must
    still publish statistics and wake the entities.
    """
    return all(
        obligation.reason is BackgroundSyncReason.HISTORY_BACKFILL
        for obligation in item.obligations
    )


def _is_gap_refill(item: BackgroundSyncItem) -> bool:
    """Report whether every obligation on an item is a hole refill.

    All, not any: a window an ordinary obligation also wants must still take the ordinary path,
    which publishes statistics and registers a checkpoint completion.
    """
    return all(
        obligation.reason is BackgroundSyncReason.GAP_REFILL for obligation in item.obligations
    )


def _gap_windows(gap: IntervalGap) -> tuple[BackgroundWindow, ...]:
    """Split one missing stretch into windows the provider will accept.

    Chunked from the start of the stretch, so the same stretch always produces the same windows.
    That is what lets an attempt be remembered against a window rather than against the stretch,
    whose boundaries move as it fills.
    """
    windows: list[BackgroundWindow] = []
    cursor = gap.start_at
    while cursor < gap.end_at:
        end_at = min(cursor + MAX_QUERY_WINDOW, gap.end_at)
        windows.append(BackgroundWindow(cursor, end_at))
        cursor = end_at
    return tuple(windows)


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
    # How far a requested walk into the past has got. Carried here because diagnostics, the
    # progress sensor, and the repair issues all need it and this is already the per-direction
    # state they read.
    backfill_state: BackfillState | None = None
    backfill_cursor: datetime | None = None
    backfill_empty_streak: int = 0


@dataclass(frozen=True, slots=True)
class HistoryGapSummary:
    """How much of one direction's collected history is missing, and over what span.

    A hole is otherwise invisible: the Energy Dashboard simply shows less. Two on one real
    installation went unnoticed for days, and the one that was eventually spotted was only
    spotted because it produced a negative figure.

    Counts and timestamps only. The identities are the installation-local HMACs used everywhere
    else in diagnostics.
    """

    account: str
    supply_point: str
    direction: ReadingDirection
    gaps: int
    missing_hours: float
    earliest_gap_at: datetime | None
    latest_gap_end_at: datetime | None


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
        return _find_supply_point(self.accounts, account_id, supply_point_id)

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
        # The instant the last poll asked the provider about. Every snapshot built outside a
        # poll is dated with it rather than with the wall clock; `_calendar_now` says why.
        self._polled_at: datetime | None = None
        self._poll_pending = False
        self._poll_idle = asyncio.Event()
        self._poll_idle.set()
        self._worker_wakeup = asyncio.Event()
        self._retry = BackgroundRetryController()
        # The last reading of the hourly point allowance, and when it was taken. Only the
        # history walk consults it; every other cadence is bounded by its own interval.
        self._allowance: PointsAllowance | None = None
        self._allowance_read_at = datetime.min.replace(tzinfo=UTC)
        self._startup_delay = (
            startup_stagger(entry.entry_id) if startup_delay is None else startup_delay
        )
        self._startup_complete = False
        self._statistics_projector = statistics_projector
        self._statistics_pending: dict[SupplyPointKey, _StatisticsPending] = {}
        # The Japanese day each supply point's history was last scanned for holes. In memory
        # only: a restart scans once more, which repairs an interrupted scan.
        self._gaps_scanned_on: dict[SupplyPointKey, date] = {}
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
                self._polled_at = now
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

    def _calendar_now(self) -> datetime:
        """Return the instant a snapshot built outside a poll should be dated with.

        A calendar bucket that runs up to "now" — today, this week, this month — reports a
        figure only when authoritative coverage reaches the snapshot's own timestamp, so that
        a period nobody has read yet says `unknown` rather than zero. A poll satisfies that by
        construction: the instant it plans its windows around is the instant it dates the
        snapshot with, and it asked the provider about everything up to it.

        Anything else has read nothing new. Dating it with the wall clock claims a later
        instant than any window covers, which fails that test and empties every running
        bucket until the next poll. Pressing the history button did exactly that: the sensors
        it was meant to fill went blank instead.

        So a snapshot with nothing new to say keeps the last poll's date. It is at most one
        poll interval old, and it is honest — that really is the last moment anything was
        asked about.
        """
        return self._polled_at or self._utc_now()

    async def _async_publish_state(self) -> None:
        """Rebuild the snapshot from what is already on disk and wake the entities.

        Reaches no provider: `_async_build_snapshot` reads ledgers already written. That is
        what makes it affordable at the two edges of a walk — the press and whatever ends it —
        and still too expensive to run for each of the hundreds of windows in between, which
        is why the cursor advance does not call it.

        Must be called without the mutation lock held; it takes the lock itself.
        """
        async with self._mutation_lock:
            snapshot = await self._async_build_snapshot(
                self._calendar_now(),
                self._enabled_states(),
                CorrectionResult(),
            )
        self.async_set_updated_data(snapshot)

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

    async def async_reprice_statistics(self) -> None:
        """Project now, because what a cost is computed from has just changed.

        The tariff and the rate adjustments arrive on their own cadence, and a cost series is
        only ever written by a statistics pass. Left to the poll, a price that has already
        arrived can sit unused for half an hour — and on a supply point that has never had a
        cost series, that is half an hour of a dashboard that shows energy and no money, with
        nothing to say whether one is coming.

        Marked from now, not from the beginning: the readings have not moved, so the energy
        rows have nothing new to say. The cost series is not limited by that mark — the
        projector republishes the whole of it by itself whenever the price, the period
        boundary, or an archived adjustment differs from what the last pass used, which is
        exactly the case that brings us here.
        """
        if self._statistics_projector is None:
            return
        now = self._utc_now()
        async with self._mutation_lock:
            for key in self._supply_points:
                if key not in self._statistics_pending:
                    self._statistics_pending[key] = _StatisticsPending(now)
            await self._async_publish_pending_statistics(now)

    def _mark_statistics_reset(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
    ) -> None:
        """Ask for one destructive rebuild of a direction's series.

        A finished walk has inserted years of hours older than everything already published, so
        every cumulative sum moves. A `dirty_from` boundary cannot express that; clearing the
        series and rebuilding it can, and it also removes rows the walk's authoritative
        reconciliation deleted. The caller publishes it; the poll is the backstop.
        """
        key = (state.supply_point.account_number, state.supply_point.id)
        pending = self._statistics_pending.get(key)
        self._statistics_pending[key] = _StatisticsPending(
            None,
            (pending.reset_directions if pending else frozenset()) | {direction},
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
                    # From `_accounts` rather than `self.data`, which is still unset during
                    # the first poll. Falling back to the calendar month there would price
                    # the first hours published against the wrong boundary.
                    billing_periods=billing_periods_for(
                        _find_supply_point(self._accounts, key[0], key[1])
                    ),
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
            self._enqueue_backfill(state)
            await self._async_enqueue_gap_refill(state)
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

    async def async_start_history_backfill(self, supply_point_identity: str) -> None:
        """Walk one supply point's readings back to where the account's history begins.

        Every direction that is queryable and not already walking is started or resumed. A
        direction the provider answers from the legacy path is refused rather than started,
        because that path returns only the most recent 31 days and the walk would record a
        month as a complete history.
        """
        state = self._state_for_identity(supply_point_identity)
        if state is None:
            return
        floor = initial_floor(self._utc_now())
        started = False
        async with self._mutation_lock:
            checkpoint = state.checkpoint
            for direction in self._previously_queryable_directions(state):
                existing = checkpoint.backfill_for(direction)
                if existing is not None and existing.state is BackfillState.RUNNING:
                    continue
                if self._reads_from_legacy(state, direction):
                    checkpoint = checkpoint.start_backfill(direction, floor).stop_backfill(
                        direction,
                        BackfillState.UNSUPPORTED,
                    )
                    continue
                checkpoint = checkpoint.start_backfill(direction, floor)
            if checkpoint != state.checkpoint:
                await state.checkpoint_backend.async_save(checkpoint.as_dict())
                state.checkpoint = checkpoint
                started = True
            self._enqueue_backfill(state)
            self._apply_checkpoint_coverage(state)
        self._ensure_background_worker()
        if started:
            # A walk runs for hours. Leaving the button's only feedback to the next poll
            # would mean up to half an hour in which pressing it appears to have done
            # nothing at all.
            await self._async_publish_state()

    def backfill_cursor(self, supply_point_identity: str) -> datetime | None:
        """Return the oldest instant any direction of one supply point has walked back to.

        Includes a direction that was refused or has stopped: its cursor still says how far
        back readings have been collected, which is what the progress sensor reports.
        """
        state = self._state_for_identity(supply_point_identity)
        if state is None:
            return None
        cursors = [
            value.cursor
            for value in state.checkpoint.backfill
            if value.state is not BackfillState.IDLE
        ]
        return min(cursors) if cursors else None

    def has_running_backfill(self, supply_point_identity: str) -> bool:
        """Report whether any direction is currently walking backwards.

        A different question from `backfill_cursor`, which a refused direction also answers.
        The button needs this one: it has to tell "started" from "refused before it began".
        """
        state = self._state_for_identity(supply_point_identity)
        if state is None:
            return False
        return any(value.state is BackfillState.RUNNING for value in state.checkpoint.backfill)

    def _history_floor(self, state: _SupplyPointRuntime) -> datetime:
        """Return the oldest instant a walk should reach for one supply point.

        When the account reports when billable supply began, that is where its readings start
        and there is nothing older to collect. Walking past it only spends requests proving the
        provider has nothing, which the empty-window rule would then conclude anyway.

        The empty-window rule still matters, and not only when this is unknown: `supplyPeriods`
        is a list, so a customer who moved out and back in has a gap between periods that the
        earliest start says nothing about.

        `BACKFILL_MAX_HISTORY` bounds it either way, so a supply start the provider reports
        wrongly cannot send the walk to 1970.
        """
        absolute = self._utc_now() - BACKFILL_MAX_HISTORY
        reported = state.supply_point.supply_start_at
        return max(reported, absolute) if reported is not None else absolute

    def _reads_from_legacy(self, state: _SupplyPointRuntime, direction: ReadingDirection) -> bool:
        observation = self._provider_observations.get(self._direction_key(state, direction))
        return observation is not None and observation.provider is ReadingProviderName.LEGACY

    async def _async_enqueue_gap_refill(self, state: _SupplyPointRuntime) -> None:
        """Queue a request for every stretch of this supply point's history that holds nothing.

        Which stretches are missing is read off the ledger, so nothing has to remember them. What
        is remembered is which windows have already been asked for and answered with nothing —
        without that the queue would ask forever for a stretch the provider does not have.

        Scanned once per Japanese day. Finding the stretches means reading every stored month, and
        a hole that has waited weeks can wait until tomorrow; doing it every refresh would parse
        the whole ledger twice an hour. The marker is in memory, so a restart scans once more,
        which is how it repairs itself if a scan was interrupted.
        """
        key = (state.supply_point.account_number, state.supply_point.id)
        now = self._utc_now()
        today = now.astimezone(TOKYO).date()
        if self._gaps_scanned_on.get(key) == today:
            return
        partitions = state.ledger.known_partitions
        if not partitions:
            self._gaps_scanned_on[key] = today
            return
        start_at, _ = partition_bounds(min(partitions))
        _, end_at = partition_bounds(max(partitions))
        try:
            records = await state.ledger.async_records(start_at, end_at)
        except LedgerError:
            # A corrupt partition is reported on its own. Leaving the marker unset retries the
            # scan on the next refresh rather than skipping the day.
            return
        self._gaps_scanned_on[key] = today
        queued = 0
        for gap in interval_gaps(records, until=now - HISTORY_GAP_GRACE):
            for window in _gap_windows(gap):
                if state.checkpoint.skips_gap_window(gap.direction, window, now):
                    continue
                self._background_queue.enqueue(
                    BackgroundSyncScope(self._status_identity(state), gap.direction, window),
                    SyncObligation(BackgroundSyncReason.GAP_REFILL, GAP_REFILL_GENERATION),
                )
                queued += 1
        if queued:
            _LOGGER.debug(
                "Queued %d window(s) to refill holes in one supply point's history",
                queued,
            )

    async def _async_complete_gap_refill(
        self,
        state: _SupplyPointRuntime,
        item: BackgroundSyncItem,
        direction_result: DirectionReadingResult,
    ) -> None:
        """Store one refilled window and remember whether it produced anything.

        A window that produced readings drops its record, because the stretch it covered is no
        longer missing. One that produced nothing has its count raised, and after three the
        provider is taken at its word for a month.

        Statistics are marked from the earliest change rather than reset: this only ever inserts,
        and inserting an hour moves every cumulative after it, which is exactly what publishing
        from that hour onwards does.
        """
        if direction_result.provider is ReadingProviderName.LEGACY:
            # The legacy path answers with the most recent 31 days however wide the window, so
            # its silence about an older one is not the provider saying there is nothing there.
            return
        async with self._mutation_lock:
            correction = await self._async_reconcile_result(
                state,
                direction_result,
                item.scope.window.start_at,
                item.scope.window.end_at,
            )
            await state.backend.async_flush()
            checkpoint = state.checkpoint.record_gap_attempt(
                item.scope.direction,
                item.scope.window,
                filled=bool(direction_result.readings),
                at=self._utc_now(),
            )
            await state.checkpoint_backend.async_save(checkpoint.as_dict())
            state.checkpoint = checkpoint
            if correction.changed:
                self._mark_statistics_dirty(correction)
                await self._async_publish_pending_statistics(self._utc_now())

    def _enqueue_backfill(self, state: _SupplyPointRuntime) -> None:
        """Queue the one window each running direction is currently owed."""
        for record in state.checkpoint.backfill:
            if record.state is not BackfillState.RUNNING:
                continue
            window = _backfill_window(record.cursor)
            self._background_queue.enqueue(
                BackgroundSyncScope(self._status_identity(state), record.direction, window),
                SyncObligation(BackgroundSyncReason.HISTORY_BACKFILL, BACKFILL_GENERATION),
            )

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
                if _is_gap_refill(item):
                    await self._async_complete_gap_refill(state, item, direction_result)
                    self._retry.resolve(item.scope)
                    self._background_active_scope = None
                    continue
                if _is_backfill(item):
                    await self._async_advance_backfill(state, item, direction_result)
                    self._retry.resolve(item.scope)
                    self._background_active_scope = None
                    continue
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
                        self._calendar_now(),
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

    async def _async_advance_backfill(
        self,
        state: _SupplyPointRuntime,
        item: BackgroundSyncItem,
        direction_result: DirectionReadingResult,
    ) -> None:
        """Store one walked window, move the cursor, and queue the next at a polite pace.

        A window that only moves the cursor deliberately does not project statistics, rebuild
        the snapshot, or wake the entities. Each of those reads the whole ledger or every
        enabled supply point, and a walk is hundreds of windows, so doing them per window is
        what would make this unusable. Until the walk ends the readings are durable in the
        ledger, which is the only place they need to be.

        Whatever ends the walk does all three, once.
        """
        direction = item.scope.direction
        window = item.scope.window
        if direction_result.provider is ReadingProviderName.LEGACY:
            # The legacy path answers with the most recent 31 days however wide the window, so
            # its answer for an older one says nothing. Advancing on it would record coverage
            # the account does not have and then declare a month a complete history.
            async with self._mutation_lock:
                await self._async_store_backfill_checkpoint(
                    state,
                    state.checkpoint.stop_backfill(direction, BackfillState.UNSUPPORTED),
                )
            await self._async_publish_state()
            return

        async with self._mutation_lock:
            await self._async_reconcile_result(
                state,
                direction_result,
                window.start_at,
                window.end_at,
            )
            await state.backend.async_flush()
            checkpoint = state.checkpoint.advance_backfill(
                direction,
                window,
                empty=not direction_result.readings,
                empty_limit=BACKFILL_EMPTY_WINDOWS,
                history_floor=self._history_floor(state),
            )
            await self._async_store_backfill_checkpoint(state, checkpoint)
            record = checkpoint.backfill_for(direction)
            if record is not None and record.state is BackfillState.RUNNING:
                self._enqueue_backfill(state)
                await self._async_pace_backfill(state, direction, record.cursor)
                return
            # The walk is done. One projection covers everything it collected, and it runs
            # here rather than waiting for the poll: the collected history is the whole point
            # of pressing the button, and a walk that has already taken hours should not owe
            # the user another half hour before its result reaches the Energy Dashboard.
            self._mark_statistics_reset(state, direction)
            await self._async_publish_pending_statistics(self._utc_now())
        await self._async_publish_state()

    async def _async_store_backfill_checkpoint(
        self,
        state: _SupplyPointRuntime,
        checkpoint: SyncCheckpoint,
    ) -> None:
        if checkpoint == state.checkpoint:
            return
        await state.checkpoint_backend.async_save(checkpoint.as_dict())
        state.checkpoint = checkpoint
        self._apply_checkpoint_coverage(state)

    async def _async_pace_backfill(
        self,
        state: _SupplyPointRuntime,
        direction: ReadingDirection,
        cursor: datetime,
    ) -> None:
        """Hold the next window back so a long walk stays a small share of the allowance."""
        now = self._utc_now()
        scope = BackgroundSyncScope(
            self._status_identity(state),
            direction,
            _backfill_window(cursor),
        )
        self._retry.defer(scope, now + BACKFILL_MIN_INTERVAL)
        allowance = await self._async_points_allowance(now)
        if allowance is not None and allowance.exhausted(BACKFILL_POINT_RESERVE):
            self._retry.defer(scope, allowance.resets_at)

    async def _async_points_allowance(self, now: datetime) -> PointsAllowance | None:
        """Return what is left of the hourly allowance, asking at most once a minute.

        Asked rather than assumed, because a fixed interval derived from the measured cost per
        request is still wrong on an account spending its allowance somewhere else. A failure
        to read it is not a reason to stop: the fixed interval alone already keeps the walk to
        about a third of the allowance.
        """
        if self._allowance is not None and now - self._allowance_read_at < _ALLOWANCE_MAX_AGE:
            return self._allowance
        try:
            self._allowance = await async_fetch_points_allowance(self._client)
        except OejpError:
            return None
        self._allowance_read_at = now
        return self._allowance

    async def _async_resolve_permanent_failure(
        self,
        state: _SupplyPointRuntime,
        item: BackgroundSyncItem,
        error_class: DirectionErrorClass,
    ) -> None:
        """Persist a generation-scoped failure and publish without retry spin."""
        if _is_gap_refill(item):
            # Not recorded as a failed window: the obligation is not a registered generation, and
            # the stretch is still missing, so the next daily scan asks again. A failure is not
            # the provider saying there is nothing there.
            self._record_direction_failure(
                state,
                item.scope.direction,
                error_class,
                queryable=False,
            )
            await self._async_publish_state()
            return
        if _is_backfill(item):
            # `mark_failed` validates against a registered generation window, and a backfill
            # has none. The cursor stays where it is, so pressing again resumes.
            async with self._mutation_lock:
                await self._async_store_backfill_checkpoint(
                    state,
                    state.checkpoint.stop_backfill(
                        item.scope.direction,
                        BackfillState.FAILED,
                        error_class.value,
                    ),
                )
            await self._async_publish_state()
            return
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
                self._calendar_now(),
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

    def abandoned_gap_windows(self) -> list[dict[str, Any]]:
        """Report the windows the provider is being taken at its word about.

        A history that stays short needs an explanation. Without this the only visible fact is
        that a stretch is missing, and nothing says the provider has already been asked three
        times and answered with nothing.
        """
        now = self._utc_now()
        return [
            {
                "supply_point": self._status_identity(state),
                "direction": attempt.direction.value,
                "start_at": attempt.start_at.isoformat(),
                "end_at": attempt.end_at.isoformat(),
                "empty_attempts": attempt.empty_streak,
                "last_attempt_at": attempt.last_attempt_at.isoformat(),
            }
            for state in self._supply_points.values()
            for attempt in state.checkpoint.abandoned_gap_windows(now)
        ]

    def extrapolated_adder_hours(self) -> list[dict[str, Any]]:
        """Report how many of each supply point's priced hours needed an extrapolated adder.

        Only supply points with at least one such hour are listed. A tariff priced from an
        agreement that has already ended is priced from rates the archive was never running to
        observe live, so its two per-kWh adders may fall back to the nearest value the archive
        does hold — which is what this counts, so that approximation is visible rather than
        assumed. See `SupplyPointTariff.is_estimate`.
        """
        if self._statistics_projector is None:
            return []
        results: list[dict[str, Any]] = []
        for state in self._supply_points.values():
            count = self._statistics_projector.extrapolated_adder_hours(
                state.supply_point.account_number,
                state.supply_point.id,
            )
            if not count:
                continue
            results.append(
                {
                    "supply_point": self._status_identity(state),
                    "extrapolated_adder_hours": count,
                }
            )
        return results

    def baseline_adder_hours(self) -> list[dict[str, Any]]:
        """Report how many of each supply point's priced hours came from the shipped baseline.

        Only supply points with at least one such hour are listed. Distinct from
        `extrapolated_adder_hours`: a baseline-covered hour has a real published rate behind
        it — see `adder_baseline.py` — it simply was not this account's own observation.
        """
        if self._statistics_projector is None:
            return []
        results: list[dict[str, Any]] = []
        for state in self._supply_points.values():
            count = self._statistics_projector.baseline_adder_hours(
                state.supply_point.account_number,
                state.supply_point.id,
            )
            if not count:
                continue
            results.append(
                {
                    "supply_point": self._status_identity(state),
                    "baseline_adder_hours": count,
                }
            )
        return results

    async def async_history_gaps(self) -> tuple[HistoryGapSummary, ...]:
        """Report the holes in each supply point's collected history.

        Computed on demand rather than every refresh: finding them means reading every stored
        month, and a diagnostics download is something a user asks for once. Doing it on the
        poll would parse the whole ledger twice an hour for a number nothing acts on yet.

        A hole is not currently refilled — see issue #98. This exists so that one can be seen
        at all, which is what a bug report about a short-looking dashboard needs.
        """
        summaries: list[HistoryGapSummary] = []
        limit = self._utc_now() - HISTORY_GAP_GRACE
        for state in self._supply_points.values():
            partitions = state.ledger.known_partitions
            if not partitions:
                continue
            start_at, _ = partition_bounds(min(partitions))
            _, end_at = partition_bounds(max(partitions))
            try:
                records = await state.ledger.async_records(start_at, end_at)
            except LedgerError:
                # A corrupt partition is already reported on its own. Refusing the whole
                # download over it would remove the rest of the evidence with it.
                continue
            by_direction: dict[ReadingDirection, list[IntervalGap]] = {}
            for gap in interval_gaps(records, until=limit):
                by_direction.setdefault(gap.direction, []).append(gap)
            for direction, gaps in by_direction.items():
                summaries.append(
                    HistoryGapSummary(
                        account=stable_account_identity(
                            self._identity_secret,
                            state.supply_point.account_number,
                        ),
                        supply_point=self._status_identity(state),
                        direction=direction,
                        gaps=len(gaps),
                        missing_hours=round(sum(gap.duration_hours for gap in gaps), 2),
                        earliest_gap_at=min(gap.start_at for gap in gaps),
                        latest_gap_end_at=max(gap.end_at for gap in gaps),
                    )
                )
        return tuple(
            sorted(summaries, key=lambda value: (value.supply_point, value.direction.value))
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
            record = state.checkpoint.backfill_for(direction)
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
                backfill_state=record.state if record else None,
                backfill_cursor=record.cursor if record else None,
                backfill_empty_streak=record.empty_streak if record else 0,
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
        if not previous.queryable and previous.error_class is not None:
            # Pairs with the warning in `_record_direction_failure`. A log that says a
            # direction went away and never says it came back leaves the reader believing
            # it is still down.
            _LOGGER.info(
                "OEJP %s readings are available again for one supply point",
                direction.value,
            )
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
        now_queryable = previous.queryable if queryable is None else queryable
        if previous.queryable and not now_queryable:
            # Every entity on this direction goes unavailable here. A poll where some
            # directions still succeed does not raise, so Home Assistant logs nothing of its
            # own, and this used to be the whole record: fifteen sensors turning unavailable
            # with no line anywhere saying why. Logged on the transition rather than on every
            # poll, so a direction that stays down says it once.
            _LOGGER.warning(
                "OEJP %s readings are no longer available for one supply point (%s). "
                "Its sensors will report unavailable until a later attempt succeeds",
                direction.value,
                error_class.value,
            )
        self._direction_statuses[key] = DirectionSyncStatus(
            account_identity=previous.account_identity,
            supply_point_identity=previous.supply_point_identity,
            direction=direction,
            queryable=now_queryable,
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


def _find_supply_point(
    accounts: Iterable[OejpAccount],
    account_id: str,
    supply_point_id: str,
) -> OejpSupplyPoint | None:
    for account in accounts:
        if account.number != account_id:
            continue
        for point in iter_supply_points(account):
            if point.id == supply_point_id:
                return point
    return None


def _backfill_window(cursor: datetime) -> BackgroundWindow:
    """Return the window immediately before a cursor.

    Derived rather than planned. A five-year walk is hundreds of windows per direction, and
    registering them on the checkpoint would grow it without bound; the cursor is enough to say
    which one is owed next, and re-fetching one costs nothing because the ledger is keyed.
    """
    return BackgroundWindow(cursor - MAX_QUERY_WINDOW, cursor)


def billing_periods_for(point: OejpSupplyPoint | None) -> BillingPeriodCalendar:
    """Return the periods one supply point's stepped charges accumulate over.

    Whichever the provider reports states the reading schedule most directly wins. Two
    consecutive scheduled reading dates that agree on a day, one month apart, are the schedule
    itself. The day billable supply began lands on the read day only if service happened to
    start on one, so it is the weaker evidence. With neither, the Asia/Tokyo calendar month is
    used, which is what the cost formula did before either was read.
    """
    if point is None:
        return BillingPeriodCalendar.calendar_months(TOKYO)
    if point.reading_schedule_day is not None:
        return BillingPeriodCalendar.from_reading_day(
            point.reading_schedule_day,
            local_timezone=TOKYO,
        )
    if point.supply_start_at is not None:
        return BillingPeriodCalendar.from_supply_start(point.supply_start_at, local_timezone=TOKYO)
    return BillingPeriodCalendar.calendar_months(TOKYO)


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
