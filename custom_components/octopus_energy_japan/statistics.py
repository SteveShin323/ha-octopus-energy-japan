"""Deterministic hourly statistics projections from the interval ledger."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .aggregation import AggregatedInterval, aggregate_intervals
from .api import ReadingDirection
from .ledger import LedgerRecord


class StatisticKind(StrEnum):
    """Supported external statistic value families."""

    ENERGY = "energy"
    OFFICIAL_COST = "official_cost"


@dataclass(frozen=True, slots=True)
class StatisticsSeriesKey:
    """Provider-facing identity of one external statistics series."""

    account_id: str
    supply_point_id: str
    direction: ReadingDirection
    kind: StatisticKind


@dataclass(frozen=True, slots=True)
class HourlyStatistic:
    """One UTC-aligned hourly value and cumulative sum."""

    start: datetime
    state: Decimal
    sum: Decimal

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("Statistic timestamps must be timezone-aware")
        normalized = self.start.astimezone(UTC)
        if normalized.minute or normalized.second or normalized.microsecond:
            raise ValueError("Statistic timestamps must be aligned to a UTC hour")
        object.__setattr__(self, "start", normalized)


@dataclass(frozen=True, slots=True)
class StatisticsSeriesProjection:
    """Ordered hourly values for one energy or official-cost series."""

    key: StatisticsSeriesKey
    statistics: tuple[HourlyStatistic, ...]


@dataclass(frozen=True, slots=True)
class StatisticsProjection:
    """All series affected by one deterministic projection pass."""

    series: tuple[StatisticsSeriesProjection, ...]
    generated_at: datetime


@dataclass(slots=True)
class _HourAccumulator:
    energy: Decimal = Decimal(0)
    official_cost: Decimal = Decimal(0)
    has_official_cost: bool = False
    official_cost_complete: bool = True


type _EnergyKey = tuple[str, str, ReadingDirection, datetime]


def project_hourly_statistics(
    records: tuple[LedgerRecord, ...],
    generated_at: datetime,
    *,
    dirty_from: datetime | None = None,
) -> StatisticsProjection:
    """Project hourly energy and provider-issued cost with deterministic sums.

    Cumulative sums are always calculated from every supplied ledger record.
    ``dirty_from`` only limits the returned replacement rows, so a correction
    can update the affected hour and every later sum without rewriting earlier
    recorder rows.
    """
    end_at = _utc(generated_at)
    dirty_hour = _hour_start(dirty_from) if dirty_from is not None else None
    hourly: dict[_EnergyKey, _HourAccumulator] = defaultdict(_HourAccumulator)
    for interval in aggregate_intervals(records):
        if interval.end_at > end_at:
            continue
        _allocate_interval(hourly, interval)

    energy_by_series: dict[StatisticsSeriesKey, list[tuple[datetime, Decimal]]] = defaultdict(list)
    cost_by_series: dict[StatisticsSeriesKey, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for (account_id, supply_point_id, direction, hour), value in sorted(
        hourly.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            item[0][2].value,
            item[0][3],
        ),
    ):
        energy_by_series[
            StatisticsSeriesKey(
                account_id,
                supply_point_id,
                direction,
                StatisticKind.ENERGY,
            )
        ].append((hour, value.energy))
        if value.has_official_cost and value.official_cost_complete:
            cost_by_series[
                StatisticsSeriesKey(
                    account_id,
                    supply_point_id,
                    direction,
                    StatisticKind.OFFICIAL_COST,
                )
            ].append((hour, value.official_cost))

    projections = [
        _project_series(key, values, dirty_hour)
        for collection in (energy_by_series, cost_by_series)
        for key, values in collection.items()
    ]
    return StatisticsProjection(
        tuple(
            sorted(
                projections,
                key=lambda item: (
                    item.key.account_id,
                    item.key.supply_point_id,
                    item.key.direction.value,
                    item.key.kind.value,
                ),
            )
        ),
        end_at,
    )


def _allocate_interval(
    hourly: dict[_EnergyKey, _HourAccumulator],
    interval: AggregatedInterval,
) -> None:
    start = interval.start_at.astimezone(UTC)
    end = interval.end_at.astimezone(UTC)
    duration = Decimal(str((end - start).total_seconds()))
    cursor = _hour_start(start)
    while cursor < end:
        boundary = cursor + timedelta(hours=1)
        overlap_start = max(start, cursor)
        overlap_end = min(end, boundary)
        overlap = Decimal(str((overlap_end - overlap_start).total_seconds()))
        ratio = overlap / duration
        key = (
            interval.account_id,
            interval.supply_point_id,
            interval.direction,
            cursor,
        )
        value = hourly[key]
        value.energy += interval.energy_kwh * ratio
        if interval.official_cost is None:
            value.official_cost_complete = False
        else:
            value.has_official_cost = True
            value.official_cost += interval.official_cost * ratio
        cursor = boundary


def _project_series(
    key: StatisticsSeriesKey,
    values: list[tuple[datetime, Decimal]],
    dirty_hour: datetime | None,
) -> StatisticsSeriesProjection:
    cumulative = Decimal(0)
    projected: list[HourlyStatistic] = []
    for start, state in sorted(values):
        cumulative += state
        if dirty_hour is None or start >= dirty_hour:
            projected.append(HourlyStatistic(start, state, cumulative))
    return StatisticsSeriesProjection(key, tuple(projected))


def _hour_start(value: datetime) -> datetime:
    normalized = _utc(value)
    return normalized.replace(minute=0, second=0, microsecond=0)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Statistics timestamps must be timezone-aware")
    return value.astimezone(UTC)
