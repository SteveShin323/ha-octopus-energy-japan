"""Rate-conscious polling, backfill, and reconciliation window planning."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from random import Random
from zoneinfo import ZoneInfo

TOKYO = ZoneInfo("Asia/Tokyo")
POLL_INTERVAL = timedelta(minutes=30)
POLL_OVERLAP = timedelta(hours=72)
MAX_QUERY_WINDOW = timedelta(days=7)
DISCOVERY_INTERVAL = timedelta(hours=24)
CONTRACT_INTERVAL = timedelta(hours=12)
BILLING_INTERVAL = timedelta(hours=12)
MAX_BACKOFF = timedelta(hours=1)

# The history backfill's pace. Measured against a real account: one reading request costs a
# flat 17 points of a 50,000-per-hour allowance whatever page size it asks for, so one request
# every three seconds draws about a third of the allowance and leaves the rest for the ordinary
# poll. It is a floor rather than a schedule — the point reserve below is what actually stops
# the walk if anything else on the account is spending.
BACKFILL_MIN_INTERVAL = timedelta(seconds=3)
# Stop walking while fewer than this many points remain in the current hour, and wait for the
# allowance to reset. Two-fifths of it is kept for everything that is not a backfill.
BACKFILL_POINT_RESERVE = 20_000
# How many consecutive empty windows end the walk. Twenty-one days of silence: one empty window
# is not evidence, because a meter exchange, a move with a supply gap, or a provider gap each
# produce one.
BACKFILL_EMPTY_WINDOWS = 3
# An absolute floor, so a defect cannot walk to 1970. Ten years is far past anything the
# provider has been asked for.
BACKFILL_MAX_HISTORY = timedelta(days=3660)


class SyncReason(StrEnum):
    """Why one bounded reading query was scheduled."""

    POLL = "poll"
    INITIAL = "initial"
    DAILY_RECONCILIATION = "daily_reconciliation"


@dataclass(frozen=True, slots=True)
class SyncWindow:
    """One bounded, half-open UTC query window."""

    start_at: datetime
    end_at: datetime
    reason: SyncReason

    def __post_init__(self) -> None:
        start = _utc(self.start_at)
        end = _utc(self.end_at)
        if end <= start:
            raise ValueError("Sync window end must be later than start")
        if end - start > MAX_QUERY_WINDOW:
            raise ValueError("Sync window exceeds the seven-day query limit")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


@dataclass(frozen=True, slots=True)
class SyncScheduleState:
    """Persistable timestamps controlling slow-cadence operations."""

    last_reconciliation_date: date | None = None
    last_discovery_at: datetime | None = None
    last_contract_at: datetime | None = None
    last_billing_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SlowCadenceDue:
    """Slow operations due at a particular coordinator refresh."""

    discovery: bool
    contract: bool
    billing: bool
    reconciliation: bool


class SyncWindowPlanner:
    """Create deterministic, non-overlapping API query chunks."""

    def __init__(
        self,
        *,
        timezone: ZoneInfo = TOKYO,
        max_query_window: timedelta = MAX_QUERY_WINDOW,
    ) -> None:
        if max_query_window <= timedelta(0) or max_query_window > MAX_QUERY_WINDOW:
            raise ValueError("max_query_window must be between zero and seven days")
        self._timezone = timezone
        self._max_query_window = max_query_window

    def poll(self, now: datetime) -> tuple[SyncWindow, ...]:
        """Plan the regular 72-hour overlap query."""
        end = _utc(now)
        return self._chunk(end - POLL_OVERLAP, end, SyncReason.POLL)

    def initial(self, now: datetime) -> tuple[SyncWindow, ...]:
        """Plan current and previous local calendar month backfill."""
        end = _utc(now)
        local_month = end.astimezone(self._timezone).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return self._chunk(
            _shift_month(local_month, -1).astimezone(UTC),
            end,
            SyncReason.INITIAL,
        )

    def reconciliation(self, now: datetime) -> tuple[SyncWindow, ...]:
        """Plan the daily current/previous local month authoritative refresh."""
        end = _utc(now)
        local_month = end.astimezone(self._timezone).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return self._chunk(
            _shift_month(local_month, -1).astimezone(UTC),
            end,
            SyncReason.DAILY_RECONCILIATION,
        )

    def _chunk(
        self,
        start_at: datetime,
        end_at: datetime,
        reason: SyncReason,
    ) -> tuple[SyncWindow, ...]:
        start = _utc(start_at)
        end = _utc(end_at)
        if end <= start:
            raise ValueError("Sync plan end must be later than start")
        windows: list[SyncWindow] = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + self._max_query_window, end)
            windows.append(SyncWindow(cursor, chunk_end, reason))
            cursor = chunk_end
        return tuple(windows)


def slow_cadence_due(
    now: datetime,
    state: SyncScheduleState,
    *,
    timezone: ZoneInfo = TOKYO,
) -> SlowCadenceDue:
    """Determine slow operations without coupling them to consumption polling."""
    current = _utc(now)
    local_date = current.astimezone(timezone).date()
    return SlowCadenceDue(
        discovery=_is_due(current, state.last_discovery_at, DISCOVERY_INTERVAL),
        contract=_is_due(current, state.last_contract_at, CONTRACT_INTERVAL),
        billing=_is_due(current, state.last_billing_at, BILLING_INTERVAL),
        reconciliation=state.last_reconciliation_date != local_date,
    )


def exponential_backoff(
    attempt: int,
    *,
    retry_after: timedelta | None = None,
    base: timedelta = timedelta(seconds=30),
    maximum: timedelta = MAX_BACKOFF,
    jitter_seed: str = "",
) -> timedelta:
    """Return bounded exponential backoff with deterministic full jitter."""
    if attempt < 0:
        raise ValueError("Backoff attempt must not be negative")
    if base <= timedelta(0) or maximum <= timedelta(0):
        raise ValueError("Backoff durations must be positive")
    if retry_after is not None:
        if retry_after < timedelta(0):
            raise ValueError("Retry-After must not be negative")
        return min(retry_after, maximum)
    cap_seconds = min(
        base.total_seconds() * (2**attempt),
        maximum.total_seconds(),
    )
    seed = hashlib.sha256(f"{jitter_seed}:{attempt}".encode()).digest()
    random = Random(int.from_bytes(seed[:8]))
    return timedelta(seconds=random.uniform(0, cap_seconds))


def startup_stagger(identity: str, *, maximum: timedelta = timedelta(minutes=5)) -> timedelta:
    """Spread coordinator startup without exposing or transmitting the identity."""
    if not identity:
        raise ValueError("Stagger identity must not be empty")
    if maximum <= timedelta(0):
        raise ValueError("Stagger maximum must be positive")
    digest = hashlib.sha256(identity.encode()).digest()
    fraction = int.from_bytes(digest[:8]) / ((1 << 64) - 1)
    return timedelta(seconds=maximum.total_seconds() * fraction)


def _is_due(
    now: datetime,
    previous: datetime | None,
    interval: timedelta,
) -> bool:
    if previous is None:
        return True
    return now - _utc(previous) >= interval


def _shift_month(value: datetime, offset: int) -> datetime:
    month_index = value.year * 12 + (value.month - 1) + offset
    year, zero_based_month = divmod(month_index, 12)
    return value.replace(year=year, month=zero_based_month + 1, day=1)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Sync timestamp must be timezone-aware")
    return value.astimezone(UTC)
