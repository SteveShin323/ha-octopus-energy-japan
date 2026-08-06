"""Pure background retry and availability scheduling state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .background_sync import BackgroundSyncItem, BackgroundSyncScope
from .sync import exponential_backoff

MAX_BACKGROUND_ATTEMPTS = 5
BACKGROUND_DEFER = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class RetrySchedule:
    """Result of recording one transient background failure."""

    not_before: datetime
    activation_attempt: int
    deferred: bool


@dataclass(frozen=True, slots=True)
class AvailableWork:
    """Either one ready item or the earliest future activation time."""

    item: BackgroundSyncItem | None
    not_before: datetime | None = None


class BackgroundRetryController:
    """Keep item-local retry state and an entry-wide rate-limit barrier."""

    def __init__(self) -> None:
        self._attempts: dict[BackgroundSyncScope, int] = {}
        self._item_not_before: dict[BackgroundSyncScope, datetime] = {}
        self._entry_not_before: datetime | None = None

    @property
    def entry_not_before(self) -> datetime | None:
        """Return the active entry-wide rate-limit barrier."""
        return self._entry_not_before

    def available(
        self,
        items: tuple[BackgroundSyncItem, ...],
        now: datetime,
    ) -> AvailableWork:
        """Select the first priority-ordered ready item without mutating the queue."""
        current = _utc(now)
        earliest: datetime | None = None
        for item in items:
            not_before = self._effective_not_before(item.scope)
            if not_before is None or not_before <= current:
                return AvailableWork(item)
            earliest = not_before if earliest is None else min(earliest, not_before)
        return AvailableWork(None, earliest)

    def record_transient(
        self,
        scope: BackgroundSyncScope,
        now: datetime,
        *,
        retry_after: timedelta | None,
        rate_limited: bool,
    ) -> RetrySchedule:
        """Schedule bounded deterministic retry or a six-hour deferral."""
        current = _utc(now)
        attempt = self._attempts.get(scope, 0) + 1
        if attempt >= MAX_BACKGROUND_ATTEMPTS:
            attempt = 0
            delay = BACKGROUND_DEFER
            deferred = True
        else:
            delay = exponential_backoff(
                attempt - 1,
                retry_after=retry_after,
                jitter_seed=_scope_seed(scope),
            )
            deferred = False
        not_before = current + delay
        self._attempts[scope] = attempt
        self._item_not_before[scope] = not_before
        if rate_limited:
            self._entry_not_before = max(
                (value for value in (self._entry_not_before, not_before) if value is not None),
            )
        return RetrySchedule(not_before, attempt, deferred)

    def defer(self, scope: BackgroundSyncScope, not_before: datetime) -> None:
        """Hold one scope back until an instant, without touching its retry count.

        Pacing, not backoff: a successful backfill window uses this to space the next request.
        `_effective_not_before` already takes the maximum against the entry-wide rate-limit
        barrier, so the two compose without either knowing about the other, and `available`
        already skips a held scope in favour of a lower-priority ready one.
        """
        current = _utc(not_before)
        existing = self._item_not_before.get(scope)
        # Never move a barrier earlier: a backoff in progress outranks a pacing delay.
        self._item_not_before[scope] = max(existing, current) if existing else current

    def resolve(self, scope: BackgroundSyncScope) -> None:
        """Clear retry state after success or permanent resolution."""
        self._attempts.pop(scope, None)
        self._item_not_before.pop(scope, None)

    def prune(self, active_scopes: frozenset[BackgroundSyncScope]) -> None:
        """Drop retry state for obligations no longer present in the queue.

        Both maps, not only the attempt counts: a scope that was paced but never failed has no
        attempt count, so iterating that map alone leaked its barrier forever.
        """
        for scope in tuple(self._attempts) + tuple(self._item_not_before):
            if scope not in active_scopes:
                self.resolve(scope)

    def _effective_not_before(self, scope: BackgroundSyncScope) -> datetime | None:
        values = tuple(
            value
            for value in (self._entry_not_before, self._item_not_before.get(scope))
            if value is not None
        )
        return max(values) if values else None


def _scope_seed(scope: BackgroundSyncScope) -> str:
    window = scope.window
    return ":".join(
        (
            scope.supply_point_identity,
            scope.direction.value,
            window.start_at.isoformat(),
            window.end_at.isoformat(),
        )
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Retry clock must be timezone-aware")
    return value.astimezone(UTC)
