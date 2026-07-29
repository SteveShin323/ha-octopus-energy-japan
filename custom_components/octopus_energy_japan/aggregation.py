"""Deterministic Asia/Tokyo projections from authoritative ledger records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .api import EnergyUnit, ReadingDirection, ReadingSource
from .ledger import LedgerConflictError, LedgerRecord

TOKYO = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True, slots=True)
class AggregatedInterval:
    """One physical supply-point interval after source/series reconciliation."""

    account_id: str
    supply_point_id: str
    direction: ReadingDirection
    start_at: datetime
    end_at: datetime
    energy_kwh: Decimal
    official_cost: Decimal | None
    contributing_records: int
    correction_count: int


@dataclass(frozen=True, slots=True)
class PeriodAggregate:
    """Energy and optional provider-issued cost for one calendar window."""

    energy_kwh: Decimal = Decimal(0)
    official_cost: Decimal | None = None
    interval_count: int = 0
    correction_count: int = 0


@dataclass(frozen=True, slots=True)
class SupplyPointAggregation:
    """User-facing calendar projections for one supply point and direction."""

    account_id: str
    supply_point_id: str
    direction: ReadingDirection
    latest: AggregatedInterval | None
    today: PeriodAggregate
    yesterday: PeriodAggregate
    this_week: PeriodAggregate
    this_month: PeriodAggregate
    last_month: PeriodAggregate
    latest_reading_end: datetime | None
    data_delay: timedelta | None


@dataclass(frozen=True, slots=True)
class AggregationSnapshot:
    """Immutable collection of supply-point calendar projections."""

    supply_points: tuple[SupplyPointAggregation, ...]
    generated_at: datetime
    timezone: str = "Asia/Tokyo"


type _PhysicalIntervalKey = tuple[
    str,
    str,
    ReadingDirection,
    datetime,
    datetime,
]
type _TargetKey = tuple[str | None, str | None]


def aggregate_intervals(records: Iterable[LedgerRecord]) -> tuple[AggregatedInterval, ...]:
    """Select one provider generation and sum its most granular physical series."""
    grouped: dict[_PhysicalIntervalKey, list[LedgerRecord]] = defaultdict(list)
    for record in records:
        reading = record.reading
        if reading.source is ReadingSource.LEGACY_INTERVAL:
            continue
        key = (
            reading.account_id,
            reading.supply_point_id,
            reading.direction,
            reading.start_at.astimezone(UTC),
            reading.end_at.astimezone(UTC),
        )
        grouped[key].append(record)

    intervals: list[AggregatedInterval] = []
    for key in sorted(grouped, key=_physical_key_sort):
        selected = _select_physical_records(grouped[key])
        costs = [record.reading.official_cost for record in selected]
        intervals.append(
            AggregatedInterval(
                account_id=key[0],
                supply_point_id=key[1],
                direction=key[2],
                start_at=key[3],
                end_at=key[4],
                energy_kwh=sum(
                    (_energy_kwh(record) for record in selected),
                    start=Decimal(0),
                ),
                official_cost=(
                    sum((cost for cost in costs if cost is not None), start=Decimal(0))
                    if costs and all(cost is not None for cost in costs)
                    else None
                ),
                contributing_records=len(selected),
                correction_count=sum(record.correction_count for record in selected),
            )
        )
    return tuple(intervals)


def aggregate_calendar(
    records: Iterable[LedgerRecord],
    now: datetime,
    *,
    timezone: ZoneInfo = TOKYO,
) -> AggregationSnapshot:
    """Build deterministic JST day/week/month projections from ledger records."""
    generated_at = _utc(now)
    local_now = generated_at.astimezone(timezone)
    today_start = datetime.combine(local_now.date(), datetime.min.time(), timezone)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    last_month_start = _previous_month(month_start)

    intervals = aggregate_intervals(
        record for record in records if record.reading.end_at.astimezone(UTC) <= generated_at
    )
    by_supply: dict[tuple[str, str, ReadingDirection], list[AggregatedInterval]] = defaultdict(list)
    for interval in intervals:
        by_supply[
            (
                interval.account_id,
                interval.supply_point_id,
                interval.direction,
            )
        ].append(interval)

    projections: list[SupplyPointAggregation] = []
    for key in sorted(
        by_supply,
        key=lambda value: (value[0], value[1], value[2].value),
    ):
        values = by_supply[key]
        latest = max(values, key=lambda value: (value.end_at, value.start_at))
        projections.append(
            SupplyPointAggregation(
                account_id=key[0],
                supply_point_id=key[1],
                direction=key[2],
                latest=latest,
                today=_period(values, today_start, generated_at),
                yesterday=_period(values, yesterday_start, today_start),
                this_week=_period(values, week_start, generated_at),
                this_month=_period(values, month_start, generated_at),
                last_month=_period(values, last_month_start, month_start),
                latest_reading_end=latest.end_at,
                data_delay=max(generated_at - latest.end_at, timedelta(0)),
            )
        )
    return AggregationSnapshot(
        tuple(projections),
        generated_at,
        getattr(timezone, "key", str(timezone)),
    )


def _select_physical_records(records: list[LedgerRecord]) -> tuple[LedgerRecord, ...]:
    generic = [
        record for record in records if record.reading.source is ReadingSource.SUPPLY_POINT_READINGS
    ]
    candidates = generic or [
        record for record in records if record.reading.source is ReadingSource.LEGACY_HALF_HOURLY
    ]

    if generic:
        max_depth = max(_target_depth(record) for record in candidates)
        candidates = [record for record in candidates if _target_depth(record) == max_depth]

    per_target: dict[_TargetKey, LedgerRecord] = {}
    for record in candidates:
        target = (record.reading.device_id, record.reading.register_id)
        previous = per_target.get(target)
        if previous is None:
            per_target[target] = record
            continue
        previous_fetched = previous.reading.fetched_at
        current_fetched = record.reading.fetched_at
        assert previous_fetched is not None
        assert current_fetched is not None
        if current_fetched > previous_fetched:
            per_target[target] = record
        elif current_fetched == previous_fetched:
            if (
                _energy_kwh(previous) != _energy_kwh(record)
                or previous.reading.official_cost != record.reading.official_cost
            ):
                raise LedgerConflictError(
                    "Equivalent unit series conflicted at the same observation time"
                )
            if _unit_priority(record.reading.unit) > _unit_priority(previous.reading.unit):
                per_target[target] = record
    return tuple(
        per_target[target]
        for target in sorted(
            per_target,
            key=lambda value: (value[0] or "", value[1] or ""),
        )
    )


def _period(
    intervals: Iterable[AggregatedInterval],
    start: datetime,
    end: datetime,
) -> PeriodAggregate:
    start_utc = start.astimezone(UTC)
    end_utc = end.astimezone(UTC)
    selected = [
        interval
        for interval in intervals
        if interval.start_at >= start_utc and interval.end_at <= end_utc
    ]
    costs = [interval.official_cost for interval in selected]
    return PeriodAggregate(
        energy_kwh=sum(
            (interval.energy_kwh for interval in selected),
            start=Decimal(0),
        ),
        official_cost=(
            sum((cost for cost in costs if cost is not None), start=Decimal(0))
            if selected and all(cost is not None for cost in costs)
            else None
        ),
        interval_count=len(selected),
        correction_count=sum(interval.correction_count for interval in selected),
    )


def _energy_kwh(record: LedgerRecord) -> Decimal:
    value = record.reading.value
    if record.reading.unit is EnergyUnit.WH:
        return value / Decimal(1_000)
    if record.reading.unit is EnergyUnit.MWH:
        return value * Decimal(1_000)
    return value


def _target_depth(record: LedgerRecord) -> int:
    if record.reading.register_id is not None:
        return 2
    if record.reading.device_id is not None:
        return 1
    return 0


def _unit_priority(unit: EnergyUnit) -> int:
    return {
        EnergyUnit.WH: 0,
        EnergyUnit.KWH: 1,
        EnergyUnit.MWH: 2,
    }[unit]


def _previous_month(value: datetime) -> datetime:
    if value.month == 1:
        return value.replace(year=value.year - 1, month=12)
    return value.replace(month=value.month - 1)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Aggregation time must be timezone-aware")
    return value.astimezone(UTC)


def _physical_key_sort(key: _PhysicalIntervalKey) -> tuple[object, ...]:
    return (key[0], key[1], key[2].value, key[3], key[4])
