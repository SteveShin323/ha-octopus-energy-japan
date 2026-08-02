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
    LONG_BACKFILL = "long_backfill"


class BackgroundSyncPriority(IntEnum):
    """Lower values execute before larger values."""

    DAILY_RECONCILIATION = 10
    INITIAL_CURRENT_MONTH = 20
    INITIAL_PREVIOUS_MONTH = 30
    LONG_BACKFILL = 40


_PRIORITY = {
    BackgroundSyncReason.DAILY_RECONCILIATION: BackgroundSyncPriority.DAILY_RECONCILIATION,
    BackgroundSyncReason.INITIAL_CURRENT_MONTH: BackgroundSyncPriority.INITIAL_CURRENT_MONTH,
    BackgroundSyncReason.INITIAL_PREVIOUS_MONTH: BackgroundSyncPriority.INITIAL_PREVIOUS_MONTH,
    BackgroundSyncReason.LONG_BACKFILL: BackgroundSyncPriority.LONG_BACKFILL,
}


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
        previous_month = _shift_month(current_month, -1)
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
        start = _shift_month(_local_month_start(current), -1).astimezone(UTC)
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
class DirectionCoverage:
    """One merged background authoritative coverage range."""

    direction: ReadingDirection
    window: BackgroundWindow


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
    background_coverage: tuple[DirectionCoverage, ...] = ()
    daily_completed: tuple[DailyDirectionCompletion, ...] = ()
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
        return replace(
            self,
            month_pair_generation=current,
            generations=retained_generations,
            completed_windows=retained_completed,
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
        checkpoint = replace(
            self,
            generations=retained_generations,
            completed_windows=retained_completed,
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

    def enqueue_missing(
        self,
        queue: BackgroundSyncQueue,
        supply_point_identity: str,
        direction: ReadingDirection,
        generation: PlannedGeneration,
    ) -> None:
        """Reconstruct only missing windows for one direction and generation."""
        for window in generation.windows:
            if self.is_completed(direction, generation.obligation, window):
                continue
            queue.enqueue(
                BackgroundSyncScope(supply_point_identity, direction, window),
                generation.obligation,
            )

    def coverage_for(
        self,
        direction: ReadingDirection,
    ) -> tuple[BackgroundWindow, ...]:
        """Return durable background coverage for one direction."""
        return tuple(
            value.window for value in self.background_coverage if value.direction is direction
        )

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
        coverage = tuple(
            DirectionCoverage(_direction(value), _window(value))
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
        return cls(
            month_pair_generation=month_pair,
            generations=generations,
            completed_windows=tuple(sorted(set(completed), key=_completion_sort_key)),
            background_coverage=_normalize_coverage(coverage),
            daily_completed=tuple(sorted(set(daily))),
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
    window: BackgroundWindow,
) -> tuple[DirectionCoverage, ...]:
    values = [value.window for value in coverage if value.direction is direction]
    values.append(window)
    merged: list[BackgroundWindow] = []
    for value in sorted(values):
        if merged and value.start_at <= merged[-1].end_at:
            previous = merged[-1]
            merged[-1] = BackgroundWindow(
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


def _month_pair_generation(now: datetime) -> str:
    current = _local_month_start(now)
    previous = _shift_month(current, -1)
    return f"{previous:%Y-%m}_{current:%Y-%m}"


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
