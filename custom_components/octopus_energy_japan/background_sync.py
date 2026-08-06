"""Persistent background synchronization planning, queueing, and checkpoints."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from typing import Any, Protocol, Self

from .api import ReadingDirection
from .sync import MAX_QUERY_WINDOW, POLL_OVERLAP, TOKYO

CHECKPOINT_SCHEMA_VERSION = 1
_MONTH_PAIR_PATTERN = re.compile(r"\d{4}-\d{2}_\d{4}-\d{2}")


class BackgroundSyncReason(StrEnum):
    """Durable reason for one background request obligation."""

    DAILY_RECONCILIATION = "daily_reconciliation"
    INITIAL_CURRENT_MONTH = "initial_current_month"
    INITIAL_PREVIOUS_MONTH = "initial_previous_month"
    # Only ever in the in-memory queue. Its progress is a cursor on the checkpoint rather than a
    # list of planned windows, so this value is never serialized — see `DirectionBackfill`.
    HISTORY_BACKFILL = "history_backfill"


class BackgroundSyncPriority(IntEnum):
    """Lower values execute before larger values."""

    DAILY_RECONCILIATION = 10
    INITIAL_CURRENT_MONTH = 20
    INITIAL_PREVIOUS_MONTH = 30
    # Last. A walk that can run for hours must never delay the work that keeps today correct.
    HISTORY_BACKFILL = 40


_PRIORITY = {
    BackgroundSyncReason.DAILY_RECONCILIATION: BackgroundSyncPriority.DAILY_RECONCILIATION,
    BackgroundSyncReason.INITIAL_CURRENT_MONTH: BackgroundSyncPriority.INITIAL_CURRENT_MONTH,
    BackgroundSyncReason.INITIAL_PREVIOUS_MONTH: BackgroundSyncPriority.INITIAL_PREVIOUS_MONTH,
    BackgroundSyncReason.HISTORY_BACKFILL: BackgroundSyncPriority.HISTORY_BACKFILL,
}
# One generation id per direction, because a backfill has no planned windows to name.
BACKFILL_GENERATION = "backfill"


class BackfillState(StrEnum):
    """How far one direction's walk into the past has got."""

    IDLE = "idle"
    RUNNING = "running"
    # Enough consecutive empty windows to conclude the account has no older readings.
    COMPLETE = "complete"
    # The provider answered from the legacy path, which returns only the most recent 31 days.
    # Walking on would build a 31-day "full history" and then declare it finished.
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, order=True)
class DirectionBackfill:
    """How far back one direction has been walked, and whether to keep going.

    A cursor rather than a list of planned windows. A five-year walk is hundreds of windows per
    direction, and registering them would grow the checkpoint without bound and make every save
    quadratic in the validation that matches a completion to its generation. The window in
    flight is derived from the cursor instead, and re-fetching one costs nothing because the
    ledger is keyed.
    """

    direction: ReadingDirection
    state: BackfillState
    # The oldest instant reached so far. Work moves backwards from here.
    cursor: datetime
    empty_streak: int = 0
    error_class: str | None = None

    def __post_init__(self) -> None:
        if self.empty_streak < 0:
            raise ValueError("A backfill empty streak cannot be negative")
        object.__setattr__(self, "cursor", _utc(self.cursor))


@dataclass(frozen=True, slots=True, order=True)
class BackgroundWindow:
    """One half-open UTC background request window."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start_at)
        end = _utc(self.end_at)
        if end <= start:
            raise ValueError("Background window end must be later than start")
        if end - start > MAX_QUERY_WINDOW:
            raise ValueError("Background window exceeds the seven-day query limit")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


@dataclass(frozen=True, slots=True, order=True)
class CoverageWindow:
    """One merged half-open UTC coverage range without request-size limits."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start_at)
        end = _utc(self.end_at)
        if end <= start:
            raise ValueError("Coverage window end must be later than start")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


@dataclass(frozen=True, slots=True)
class SyncObligation:
    """One reason/generation satisfied by a successful request scope."""

    reason: BackgroundSyncReason
    generation: str

    def __post_init__(self) -> None:
        if not self.generation or len(self.generation) > 256:
            raise ValueError("Background generation identifier is invalid")


@dataclass(frozen=True, slots=True)
class BackgroundSyncScope:
    """Deduplicated request identity without raw provider identifiers."""

    supply_point_identity: str
    direction: ReadingDirection
    window: BackgroundWindow

    def __post_init__(self) -> None:
        if not self.supply_point_identity.startswith("supply-point-"):
            raise ValueError("Background scope requires an opaque supply-point identity")
        if self.direction not in {ReadingDirection.IMPORT, ReadingDirection.EXPORT}:
            raise ValueError("Background scope requires import or export direction")


@dataclass(frozen=True, slots=True)
class BackgroundSyncItem:
    """One request scope with every obligation coalesced onto it."""

    scope: BackgroundSyncScope
    obligations: frozenset[SyncObligation]

    def __post_init__(self) -> None:
        if not self.obligations:
            raise ValueError("Background item requires at least one obligation")

    @property
    def priority(self) -> BackgroundSyncPriority:
        """Return the strongest current obligation priority."""
        return min(_PRIORITY[obligation.reason] for obligation in self.obligations)


class BackgroundSyncQueue:
    """Deterministic in-memory queue reconstructed from durable checkpoints."""

    def __init__(self) -> None:
        self._items: dict[BackgroundSyncScope, BackgroundSyncItem] = {}

    def __len__(self) -> int:
        return len(self._items)

    def enqueue(self, scope: BackgroundSyncScope, obligation: SyncObligation) -> None:
        """Coalesce an obligation and implicitly upgrade effective priority."""
        previous = self._items.get(scope)
        obligations = (
            previous.obligations | {obligation} if previous is not None else frozenset({obligation})
        )
        self._items[scope] = BackgroundSyncItem(scope, frozenset(obligations))

    def enqueue_item(self, item: BackgroundSyncItem) -> None:
        """Restore a previously popped item without losing obligations."""
        for obligation in item.obligations:
            self.enqueue(item.scope, obligation)

    def discard(self, scope: BackgroundSyncScope) -> None:
        """Discard a completed request scope."""
        self._items.pop(scope, None)

    def retain_supply_points(self, identities: frozenset[str]) -> None:
        """Cancel queued work for disabled or missing supply points."""
        for scope in tuple(self._items):
            if scope.supply_point_identity not in identities:
                self._items.pop(scope)

    def remove_obligations(
        self,
        reason: BackgroundSyncReason,
        generations: frozenset[str],
    ) -> None:
        """Remove only obsolete obligations while retaining shared scopes."""
        for scope, item in tuple(self._items.items()):
            obligations = frozenset(
                obligation
                for obligation in item.obligations
                if not (obligation.reason is reason and obligation.generation in generations)
            )
            if obligations:
                self._items[scope] = BackgroundSyncItem(scope, obligations)
            else:
                self._items.pop(scope)

    def pop_next(self) -> BackgroundSyncItem | None:
        """Pop the highest-priority, newest, deterministic request scope."""
        if not self._items:
            return None
        item = min(self._items.values(), key=_item_sort_key)
        self._items.pop(item.scope)
        return item

    def snapshot(self) -> tuple[BackgroundSyncItem, ...]:
        """Return the queue in execution order without mutating it."""
        return tuple(sorted(self._items.values(), key=_item_sort_key))


@dataclass(frozen=True, slots=True)
class PlannedGeneration:
    """Deterministic windows and metadata for one obligation generation."""

    obligation: SyncObligation
    target_end: datetime
    windows: tuple[BackgroundWindow, ...]
    jst_date: date | None = None

    def __post_init__(self) -> None:
        target = _utc(self.target_end)
        if self.jst_date is None and (
            self.obligation.reason is BackgroundSyncReason.DAILY_RECONCILIATION
        ):
            raise ValueError("Daily generation requires a JST date")
        if self.jst_date is not None and (
            self.obligation.reason is not BackgroundSyncReason.DAILY_RECONCILIATION
        ):
            raise ValueError("Only daily generations may contain a JST date")
        object.__setattr__(self, "target_end", target)


class BackgroundSyncPlanner:
    """Plan current/previous month work without overlapping setup ownership."""

    def initial(self, now: datetime) -> tuple[PlannedGeneration, ...]:
        """Plan current month first, then previous month, newest windows first."""
        current = _utc(now)
        cutoff = current - POLL_OVERLAP
        current_month = _local_month_start(current)
        previous_month = _local_month_start(initial_floor(current))
        pair = _month_pair_generation(current)
        plans: list[PlannedGeneration] = []
        current_start = current_month.astimezone(UTC)
        if cutoff > current_start:
            plans.append(
                self._generation(
                    BackgroundSyncReason.INITIAL_CURRENT_MONTH,
                    f"initial-current:{pair}:{_timestamp_id(cutoff)}",
                    current_start,
                    cutoff,
                )
            )
        previous_start = previous_month.astimezone(UTC)
        previous_end = min(current_start, cutoff)
        if previous_end > previous_start:
            plans.append(
                self._generation(
                    BackgroundSyncReason.INITIAL_PREVIOUS_MONTH,
                    f"initial-previous:{pair}:{_timestamp_id(previous_end)}",
                    previous_start,
                    previous_end,
                )
            )
        return tuple(plans)

    def daily(self, now: datetime) -> PlannedGeneration:
        """Plan one exact previous/current-month daily completion barrier."""
        current = _utc(now)
        local_date = current.astimezone(TOKYO).date()
        start = initial_floor(current)
        generation = f"daily:{local_date.isoformat()}:{_timestamp_id(current)}"
        return PlannedGeneration(
            SyncObligation(BackgroundSyncReason.DAILY_RECONCILIATION, generation),
            current,
            _newest_first_chunks(start, current),
            local_date,
        )

    def _generation(
        self,
        reason: BackgroundSyncReason,
        generation: str,
        start_at: datetime,
        end_at: datetime,
    ) -> PlannedGeneration:
        return PlannedGeneration(
            SyncObligation(reason, generation),
            end_at,
            _newest_first_chunks(start_at, end_at),
        )


@dataclass(frozen=True, slots=True, order=True)
class DirectionWindowCompletion:
    """One durable direction/reason/generation/window completion."""

    direction: ReadingDirection
    reason: BackgroundSyncReason
    generation: str
    window: BackgroundWindow


@dataclass(frozen=True, slots=True, order=True)
class DirectionWindowFailure:
    """One durable permanent failure for an obligation generation window."""

    direction: ReadingDirection
    reason: BackgroundSyncReason
    generation: str
    window: BackgroundWindow
    error_class: str

    def __post_init__(self) -> None:
        if not self.error_class or len(self.error_class) > 64:
            raise ValueError("Background failure class is invalid")


@dataclass(frozen=True, slots=True, order=True)
class DirectionCoverage:
    """One merged background authoritative coverage range."""

    direction: ReadingDirection
    window: CoverageWindow


@dataclass(frozen=True, slots=True, order=True)
class DailyDirectionCompletion:
    """Latest fully durable daily barrier for one direction."""

    direction: ReadingDirection
    jst_date: date
    completed_through: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_through", _utc(self.completed_through))


@dataclass(frozen=True, slots=True)
class SyncCheckpoint:
    """Versioned private checkpoint state for one supply point."""

    month_pair_generation: str
    generations: tuple[PlannedGeneration, ...] = ()
    completed_windows: tuple[DirectionWindowCompletion, ...] = ()
    failed_windows: tuple[DirectionWindowFailure, ...] = ()
    background_coverage: tuple[DirectionCoverage, ...] = ()
    daily_completed: tuple[DailyDirectionCompletion, ...] = ()
    # One record per direction that has ever been asked to walk backwards. Read with a tolerant
    # helper and never referenced from `generations`, so a checkpoint written here still loads
    # on a build that predates it and one written there still loads here.
    backfill: tuple[DirectionBackfill, ...] = ()
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("Unsupported sync checkpoint schema version")
        if _MONTH_PAIR_PATTERN.fullmatch(self.month_pair_generation) is None:
            raise ValueError("Sync checkpoint month-pair generation is malformed")
        generation_keys = [
            (value.obligation.reason, value.obligation.generation) for value in self.generations
        ]
        if len(generation_keys) != len(set(generation_keys)):
            raise ValueError("Sync checkpoint contains duplicate generations")
        generations = {
            (value.obligation.reason, value.obligation.generation): value
            for value in self.generations
        }
        for completion in self.completed_windows:
            generation = generations.get((completion.reason, completion.generation))
            if generation is None or completion.window not in generation.windows:
                raise ValueError("Sync checkpoint completion has no matching generation window")
        for failure in self.failed_windows:
            generation = generations.get((failure.reason, failure.generation))
            if generation is None or failure.window not in generation.windows:
                raise ValueError("Sync checkpoint failure has no matching generation window")
        if len({value.direction for value in self.backfill}) != len(self.backfill):
            raise ValueError("Sync checkpoint contains duplicate backfill directions")

    @classmethod
    def empty(cls, now: datetime) -> Self:
        """Create a checkpoint for the active Japanese month pair."""
        return cls(_month_pair_generation(_utc(now)))

    def roll_month_pair(self, now: datetime) -> Self:
        """Remove only obsolete initial-generation completion metadata."""
        current = _month_pair_generation(_utc(now))
        if current == self.month_pair_generation:
            return self
        initial_reasons = {
            BackgroundSyncReason.INITIAL_CURRENT_MONTH,
            BackgroundSyncReason.INITIAL_PREVIOUS_MONTH,
        }
        retained_generations = tuple(
            generation
            for generation in self.generations
            if generation.obligation.reason not in initial_reasons
        )
        retained_completed = tuple(
            completion
            for completion in self.completed_windows
            if completion.reason not in initial_reasons
        )
        retained_failed = tuple(
            failure for failure in self.failed_windows if failure.reason not in initial_reasons
        )
        return replace(
            self,
            month_pair_generation=current,
            generations=retained_generations,
            completed_windows=retained_completed,
            failed_windows=retained_failed,
        )

    def register(self, generation: PlannedGeneration) -> Self:
        """Register or replace deterministic metadata for one generation."""
        generations = {
            (value.obligation.reason, value.obligation.generation): value
            for value in self.generations
        }
        key = (generation.obligation.reason, generation.obligation.generation)
        existing = generations.get(key)
        if existing is not None and existing != generation:
            raise ValueError("Sync generation identifier was reused with different metadata")
        generations[key] = generation
        return replace(
            self,
            generations=tuple(
                generations[key]
                for key in sorted(
                    generations,
                    key=lambda value: (value[0].value, value[1]),
                )
            ),
        )

    def supersede_daily(self, generation: PlannedGeneration) -> tuple[Self, frozenset[str]]:
        """Replace older daily generations without touching shared reason state."""
        if generation.obligation.reason is not BackgroundSyncReason.DAILY_RECONCILIATION:
            raise ValueError("Daily supersession requires a daily generation")
        obsolete = frozenset(
            value.obligation.generation
            for value in self.generations
            if value.obligation.reason is BackgroundSyncReason.DAILY_RECONCILIATION
            and value.obligation.generation != generation.obligation.generation
        )
        retained_generations = tuple(
            value
            for value in self.generations
            if not (
                value.obligation.reason is BackgroundSyncReason.DAILY_RECONCILIATION
                and value.obligation.generation in obsolete
            )
        )
        retained_completed = tuple(
            value
            for value in self.completed_windows
            if not (
                value.reason is BackgroundSyncReason.DAILY_RECONCILIATION
                and value.generation in obsolete
            )
        )
        retained_failed = tuple(
            value
            for value in self.failed_windows
            if not (
                value.reason is BackgroundSyncReason.DAILY_RECONCILIATION
                and value.generation in obsolete
            )
        )
        checkpoint = replace(
            self,
            generations=retained_generations,
            completed_windows=retained_completed,
            failed_windows=retained_failed,
        ).register(generation)
        return checkpoint, obsolete

    def is_completed(
        self,
        direction: ReadingDirection,
        obligation: SyncObligation,
        window: BackgroundWindow,
    ) -> bool:
        """Return whether a durable completion already satisfies this obligation."""
        return (
            DirectionWindowCompletion(
                direction,
                obligation.reason,
                obligation.generation,
                window,
            )
            in self.completed_windows
        )

    def is_failed(
        self,
        direction: ReadingDirection,
        obligation: SyncObligation,
        window: BackgroundWindow,
    ) -> bool:
        """Return whether a permanent failure resolved this exact obligation."""
        return any(
            failure.direction is direction
            and failure.reason is obligation.reason
            and failure.generation == obligation.generation
            and failure.window == window
            for failure in self.failed_windows
        )

    def mark_durable(self, item: BackgroundSyncItem) -> Self:
        """Complete all obligations and merge coverage after ledger durability."""
        completed = set(self.completed_windows)
        for obligation in item.obligations:
            generation = next(
                (value for value in self.generations if value.obligation == obligation),
                None,
            )
            if generation is None or item.scope.window not in generation.windows:
                raise ValueError("Durable completion is not a registered generation window")
            completed.add(
                DirectionWindowCompletion(
                    item.scope.direction,
                    obligation.reason,
                    obligation.generation,
                    item.scope.window,
                )
            )
        coverage = _merge_direction_coverage(
            self.background_coverage,
            item.scope.direction,
            item.scope.window,
        )
        checkpoint = replace(
            self,
            completed_windows=tuple(sorted(completed, key=_completion_sort_key)),
            background_coverage=coverage,
        )
        return checkpoint._advance_daily_barrier(item.scope.direction)

    def mark_failed(self, item: BackgroundSyncItem, error_class: str) -> Self:
        """Persist a permanent failure without claiming coverage or completion."""
        failures = set(self.failed_windows)
        for obligation in item.obligations:
            generation = next(
                (value for value in self.generations if value.obligation == obligation),
                None,
            )
            if generation is None or item.scope.window not in generation.windows:
                raise ValueError("Permanent failure is not a registered generation window")
            failures.add(
                DirectionWindowFailure(
                    item.scope.direction,
                    obligation.reason,
                    obligation.generation,
                    item.scope.window,
                    error_class,
                )
            )
        return replace(self, failed_windows=tuple(sorted(failures, key=_failure_sort_key)))

    def clear_failures(self, direction: ReadingDirection) -> Self:
        """Allow one direction to be reconsidered after new provider evidence."""
        retained = tuple(
            failure for failure in self.failed_windows if failure.direction is not direction
        )
        return self if retained == self.failed_windows else replace(self, failed_windows=retained)

    def enqueue_missing(
        self,
        queue: BackgroundSyncQueue,
        supply_point_identity: str,
        direction: ReadingDirection,
        generation: PlannedGeneration,
    ) -> None:
        """Reconstruct only missing windows for one direction and generation."""
        for window in generation.windows:
            if self.is_completed(direction, generation.obligation, window) or self.is_failed(
                direction,
                generation.obligation,
                window,
            ):
                continue
            queue.enqueue(
                BackgroundSyncScope(supply_point_identity, direction, window),
                generation.obligation,
            )

    def coverage_for(
        self,
        direction: ReadingDirection,
    ) -> tuple[CoverageWindow, ...]:
        """Return durable background coverage for one direction."""
        return tuple(
            value.window for value in self.background_coverage if value.direction is direction
        )

    def backfill_for(self, direction: ReadingDirection) -> DirectionBackfill | None:
        """Return how far one direction has been walked back, if it ever started."""
        return next((value for value in self.backfill if value.direction is direction), None)

    def start_backfill(self, direction: ReadingDirection, floor: datetime) -> Self:
        """Begin, or resume, walking one direction backwards.

        Resuming keeps the stored cursor: a walk that stopped on a failure or an unsupported
        provider carries on from where it reached rather than repeating what it already has.
        """
        existing = self.backfill_for(direction)
        cursor = existing.cursor if existing is not None else floor
        started = DirectionBackfill(direction, BackfillState.RUNNING, cursor)
        return self._with_backfill(started)

    def advance_backfill(
        self,
        direction: ReadingDirection,
        window: BackgroundWindow,
        *,
        empty: bool,
        empty_limit: int,
        history_floor: datetime,
    ) -> Self:
        """Record one walked window and decide whether the walk continues.

        The cursor moves to the window's start, and the coverage it produced is merged into the
        same range the ordinary cadence records, so the two are indistinguishable afterwards.

        Two things end a walk. `history_floor` is where the caller says there is nothing older
        — the day billable supply began, when the account reports it. A run of empty windows
        covers everything that says nothing about: an account that does not report it, and the
        gap between two supply periods for a customer who moved out and back in. One empty
        window is not evidence, because a meter exchange or a provider gap each produce one.
        """
        existing = self.backfill_for(direction)
        if existing is None:
            return self
        streak = existing.empty_streak + 1 if empty else 0
        finished = streak >= empty_limit or window.start_at <= history_floor
        advanced = replace(
            existing,
            cursor=window.start_at,
            empty_streak=streak,
            state=BackfillState.COMPLETE if finished else BackfillState.RUNNING,
        )
        coverage = _merge_direction_coverage(
            self.background_coverage,
            direction,
            CoverageWindow(window.start_at, window.end_at),
        )
        return replace(
            self._with_backfill(advanced),
            background_coverage=coverage,
        )

    def stop_backfill(
        self,
        direction: ReadingDirection,
        state: BackfillState,
        error_class: str | None = None,
    ) -> Self:
        """Stop walking one direction without moving its cursor.

        The cursor is left where it is on purpose, so pressing the button again resumes rather
        than restarting. That matters most for the unsupported case: the legacy path answers
        with only the most recent 31 days, so advancing on its answer would record coverage the
        account does not have.
        """
        existing = self.backfill_for(direction)
        if existing is None:
            return self
        return self._with_backfill(replace(existing, state=state, error_class=error_class))

    def _with_backfill(self, value: DirectionBackfill) -> Self:
        others = tuple(item for item in self.backfill if item.direction is not value.direction)
        return replace(self, backfill=tuple(sorted((*others, value))))

    def _advance_daily_barrier(self, direction: ReadingDirection) -> Self:
        completed = set(self.completed_windows)
        daily = {value.direction: value for value in self.daily_completed}
        for generation in self.generations:
            if generation.obligation.reason is not BackgroundSyncReason.DAILY_RECONCILIATION:
                continue
            required = {
                DirectionWindowCompletion(
                    direction,
                    generation.obligation.reason,
                    generation.obligation.generation,
                    window,
                )
                for window in generation.windows
            }
            if not required.issubset(completed):
                continue
            assert generation.jst_date is not None
            previous = daily.get(direction)
            candidate = DailyDirectionCompletion(
                direction,
                generation.jst_date,
                generation.target_end,
            )
            if previous is None or (
                candidate.jst_date,
                candidate.completed_through,
            ) > (
                previous.jst_date,
                previous.completed_through,
            ):
                daily[direction] = candidate
        return replace(
            self,
            daily_completed=tuple(
                daily[key] for key in sorted(daily, key=lambda value: value.value)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize without raw provider identifiers."""
        return {
            "schema_version": self.schema_version,
            "month_pair_generation": self.month_pair_generation,
            "generations": [_generation_to_dict(value) for value in self.generations],
            "completed_windows": [
                {
                    "direction": value.direction.value,
                    "reason": value.reason.value,
                    "generation": value.generation,
                    "start_at": _iso(value.window.start_at),
                    "end_at": _iso(value.window.end_at),
                }
                for value in self.completed_windows
            ],
            "failed_windows": [
                {
                    "direction": value.direction.value,
                    "reason": value.reason.value,
                    "generation": value.generation,
                    "start_at": _iso(value.window.start_at),
                    "end_at": _iso(value.window.end_at),
                    "error_class": value.error_class,
                }
                for value in self.failed_windows
            ],
            "background_coverage": [
                {
                    "direction": value.direction.value,
                    "start_at": _iso(value.window.start_at),
                    "end_at": _iso(value.window.end_at),
                }
                for value in self.background_coverage
            ],
            "daily_completed": [
                {
                    "direction": value.direction.value,
                    "jst_date": value.jst_date.isoformat(),
                    "completed_through": _iso(value.completed_through),
                }
                for value in self.daily_completed
            ],
            "backfill": [
                {
                    "direction": value.direction.value,
                    "state": value.state.value,
                    "cursor": _iso(value.cursor),
                    "empty_streak": value.empty_streak,
                    "error_class": value.error_class,
                }
                for value in self.backfill
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        """Strictly deserialize schema version 1 checkpoint state."""
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("Unsupported sync checkpoint schema version")
        month_pair = _required_string(payload, "month_pair_generation")
        generations = tuple(
            _generation_from_dict(value) for value in _required_list(payload, "generations")
        )
        completed = tuple(
            DirectionWindowCompletion(
                _direction(value),
                BackgroundSyncReason(_required_string(value, "reason")),
                _required_string(value, "generation"),
                _window(value),
            )
            for value in _required_mapping_list(payload, "completed_windows")
        )
        failed = tuple(
            DirectionWindowFailure(
                _direction(value),
                BackgroundSyncReason(_required_string(value, "reason")),
                _required_string(value, "generation"),
                _window(value),
                _required_string(value, "error_class"),
            )
            for value in _optional_mapping_list(payload, "failed_windows")
        )
        coverage = tuple(
            DirectionCoverage(_direction(value), _coverage_window(value))
            for value in _required_mapping_list(payload, "background_coverage")
        )
        daily = tuple(
            DailyDirectionCompletion(
                _direction(value),
                date.fromisoformat(_required_string(value, "jst_date")),
                _datetime(_required_string(value, "completed_through")),
            )
            for value in _required_mapping_list(payload, "daily_completed")
        )
        backfill = tuple(
            DirectionBackfill(
                _direction(value),
                BackfillState(_required_string(value, "state")),
                _datetime(_required_string(value, "cursor")),
                _count(value.get("empty_streak")),
                _optional_string(value.get("error_class")),
            )
            for value in _optional_mapping_list(payload, "backfill")
        )
        return cls(
            month_pair_generation=month_pair,
            generations=generations,
            completed_windows=tuple(sorted(set(completed), key=_completion_sort_key)),
            failed_windows=tuple(sorted(set(failed), key=_failure_sort_key)),
            background_coverage=_normalize_coverage(coverage),
            daily_completed=tuple(sorted(set(daily))),
            backfill=tuple(sorted(set(backfill))),
        )


class SyncCheckpointBackend(Protocol):
    """Persistence contract used by the background runtime and tests."""

    async def async_load(self) -> Mapping[str, Any] | None:
        """Load one private checkpoint payload."""

    async def async_save(self, payload: Mapping[str, Any]) -> None:
        """Atomically save one private checkpoint payload."""


def _item_sort_key(item: BackgroundSyncItem) -> tuple[int, float, float, str, str]:
    window = item.scope.window
    return (
        int(item.priority),
        -window.end_at.timestamp(),
        -window.start_at.timestamp(),
        item.scope.supply_point_identity,
        item.scope.direction.value,
    )


def _newest_first_chunks(start_at: datetime, end_at: datetime) -> tuple[BackgroundWindow, ...]:
    start = _utc(start_at)
    end = _utc(end_at)
    windows: list[BackgroundWindow] = []
    cursor = end
    while cursor > start:
        chunk_start = max(start, cursor - MAX_QUERY_WINDOW)
        windows.append(BackgroundWindow(chunk_start, cursor))
        cursor = chunk_start
    return tuple(windows)


def _merge_direction_coverage(
    coverage: Iterable[DirectionCoverage],
    direction: ReadingDirection,
    window: BackgroundWindow | CoverageWindow,
) -> tuple[DirectionCoverage, ...]:
    values = [value.window for value in coverage if value.direction is direction]
    values.append(CoverageWindow(window.start_at, window.end_at))
    merged: list[CoverageWindow] = []
    for value in sorted(values):
        if merged and value.start_at <= merged[-1].end_at:
            previous = merged[-1]
            merged[-1] = CoverageWindow(
                previous.start_at,
                max(previous.end_at, value.end_at),
            )
        else:
            merged.append(value)
    retained = [value for value in coverage if value.direction is not direction]
    retained.extend(DirectionCoverage(direction, value) for value in merged)
    return tuple(sorted(retained))


def _normalize_coverage(
    coverage: Iterable[DirectionCoverage],
) -> tuple[DirectionCoverage, ...]:
    result: tuple[DirectionCoverage, ...] = ()
    for value in coverage:
        result = _merge_direction_coverage(result, value.direction, value.window)
    return result


def _completion_sort_key(
    value: DirectionWindowCompletion,
) -> tuple[str, str, str, datetime, datetime]:
    return (
        value.direction.value,
        value.reason.value,
        value.generation,
        value.window.start_at,
        value.window.end_at,
    )


def _failure_sort_key(
    value: DirectionWindowFailure,
) -> tuple[str, str, str, datetime, datetime, str]:
    return (
        value.direction.value,
        value.reason.value,
        value.generation,
        value.window.start_at,
        value.window.end_at,
        value.error_class,
    )


def _month_pair_generation(now: datetime) -> str:
    current = _local_month_start(now)
    previous = _shift_month(current, -1)
    return f"{previous:%Y-%m}_{current:%Y-%m}"


def initial_floor(now: datetime) -> datetime:
    """Return the oldest instant the ordinary cadence ever plans back to.

    The start of the previous local month. Both the initial plan and the daily reconciliation
    stop here, and the history backfill starts here, so there is no gap between what the
    cadence covers and what the walk collects. It was inlined at each of those places before
    the walk existed, which is exactly how a gap gets introduced.
    """
    return _shift_month(_local_month_start(now), -1).astimezone(UTC)


def _local_month_start(now: datetime) -> datetime:
    return (
        _utc(now)
        .astimezone(TOKYO)
        .replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    )


def _shift_month(value: datetime, offset: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(month_index, 12)
    return value.replace(year=year, month=month + 1, day=1)


def _generation_to_dict(value: PlannedGeneration) -> dict[str, Any]:
    return {
        "reason": value.obligation.reason.value,
        "generation": value.obligation.generation,
        "target_end": _iso(value.target_end),
        "jst_date": value.jst_date.isoformat() if value.jst_date is not None else None,
        "windows": [
            {"start_at": _iso(window.start_at), "end_at": _iso(window.end_at)}
            for window in value.windows
        ],
    }


def _generation_from_dict(value: object) -> PlannedGeneration:
    if not isinstance(value, Mapping):
        raise ValueError("Sync generation is not an object")
    raw_date = value.get("jst_date")
    if raw_date is not None and not isinstance(raw_date, str):
        raise ValueError("Sync generation JST date is malformed")
    windows = tuple(_window(window) for window in _required_mapping_list(value, "windows"))
    return PlannedGeneration(
        SyncObligation(
            BackgroundSyncReason(_required_string(value, "reason")),
            _required_string(value, "generation"),
        ),
        _datetime(_required_string(value, "target_end")),
        windows,
        date.fromisoformat(raw_date) if raw_date is not None else None,
    )


def _window(value: Mapping[str, Any]) -> BackgroundWindow:
    return BackgroundWindow(
        _datetime(_required_string(value, "start_at")),
        _datetime(_required_string(value, "end_at")),
    )


def _coverage_window(value: Mapping[str, Any]) -> CoverageWindow:
    return CoverageWindow(
        _datetime(_required_string(value, "start_at")),
        _datetime(_required_string(value, "end_at")),
    )


def _direction(value: Mapping[str, Any]) -> ReadingDirection:
    direction = ReadingDirection(_required_string(value, "direction"))
    if direction is ReadingDirection.UNKNOWN:
        raise ValueError("Checkpoint direction must be import or export")
    return direction


def _required_mapping_list(
    payload: Mapping[str, Any],
    key: str,
) -> list[Mapping[str, Any]]:
    values = _required_list(payload, key)
    if not all(isinstance(value, Mapping) for value in values):
        raise ValueError(f"Sync checkpoint {key} is malformed")
    return [value for value in values if isinstance(value, Mapping)]


def _optional_mapping_list(
    payload: Mapping[str, Any],
    key: str,
) -> list[Mapping[str, Any]]:
    if key not in payload:
        return []
    return _required_mapping_list(payload, key)


def _required_list(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"Sync checkpoint {key} is malformed")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"Sync checkpoint {key} is malformed")
    return value


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Sync checkpoint string is malformed")
    return value


def _count(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("Sync checkpoint count is malformed")
    return value


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise ValueError("Sync checkpoint datetime is malformed") from err
    return _utc(parsed)


def _timestamp_id(value: datetime) -> str:
    return _iso(_utc(value)).replace(":", "").replace("-", "")


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Background sync timestamp must be timezone-aware")
    return value.astimezone(UTC)
