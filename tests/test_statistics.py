"""Tests for deterministic hourly statistics projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from custom_components.octopus_energy_japan.api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingSource,
)
from custom_components.octopus_energy_japan.ledger import LedgerRecord
from custom_components.octopus_energy_japan.statistics import (
    HourlyStatistic,
    StatisticKind,
    project_hourly_statistics,
)
from hypothesis import given
from hypothesis import strategies as st

NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)


def _record(
    start: datetime,
    value: str,
    *,
    cost: str | None = None,
    direction: ReadingDirection = ReadingDirection.IMPORT,
    end: datetime | None = None,
    supply_point_id: str = "SP-1",
) -> LedgerRecord:
    return LedgerRecord(
        EnergyReading(
            account_id="A-1",
            supply_point_id=supply_point_id,
            direction=direction,
            start_at=start,
            end_at=end or start + timedelta(minutes=30),
            value=Decimal(value),
            unit=EnergyUnit.KWH,
            source=ReadingSource.SUPPLY_POINT_READINGS,
            official_cost=Decimal(cost) if cost is not None else None,
            fetched_at=NOW,
        )
    )


def test_projects_hourly_energy_and_complete_official_cost() -> None:
    projection = project_hourly_statistics(
        (
            _record(datetime(2026, 8, 3, 0, tzinfo=UTC), "0.4", cost="12"),
            _record(datetime(2026, 8, 3, 0, 30, tzinfo=UTC), "0.6", cost="18"),
            _record(datetime(2026, 8, 3, 1, tzinfo=UTC), "0.5", cost="15"),
        ),
        NOW,
    )

    assert [series.key.kind for series in projection.series] == [
        StatisticKind.ENERGY,
        StatisticKind.OFFICIAL_COST,
    ]
    assert projection.series[0].statistics == (
        HourlyStatistic(datetime(2026, 8, 3, 0, tzinfo=UTC), Decimal("1.0"), Decimal("1.0")),
        HourlyStatistic(datetime(2026, 8, 3, 1, tzinfo=UTC), Decimal("0.5"), Decimal("1.5")),
    )
    assert projection.series[1].statistics == (
        HourlyStatistic(datetime(2026, 8, 3, 0, tzinfo=UTC), Decimal("30"), Decimal("30")),
        HourlyStatistic(datetime(2026, 8, 3, 1, tzinfo=UTC), Decimal("15"), Decimal("45")),
    )


def test_dirty_window_retains_cumulative_baseline() -> None:
    projection = project_hourly_statistics(
        (
            _record(datetime(2026, 8, 3, 0, tzinfo=UTC), "1"),
            _record(datetime(2026, 8, 3, 1, tzinfo=UTC), "2"),
        ),
        NOW,
        dirty_from=datetime(2026, 8, 3, 1, 20, tzinfo=UTC),
    )

    assert projection.series[0].statistics == (
        HourlyStatistic(datetime(2026, 8, 3, 1, tzinfo=UTC), Decimal("2"), Decimal("3")),
    )


def test_splits_intervals_across_utc_hour_boundaries() -> None:
    projection = project_hourly_statistics(
        (
            _record(
                datetime(2026, 8, 3, 0, 30, tzinfo=UTC),
                "2",
                cost="20",
                end=datetime(2026, 8, 3, 1, 30, tzinfo=UTC),
            ),
        ),
        NOW,
    )

    assert [value.state for value in projection.series[0].statistics] == [
        Decimal("1.0"),
        Decimal("1.0"),
    ]
    assert [value.sum for value in projection.series[0].statistics] == [
        Decimal("1.0"),
        Decimal("2.0"),
    ]


def test_omits_incomplete_cost_hour_without_losing_energy() -> None:
    projection = project_hourly_statistics(
        (
            _record(datetime(2026, 8, 3, 0, tzinfo=UTC), "0.4", cost="12"),
            _record(datetime(2026, 8, 3, 0, 30, tzinfo=UTC), "0.6"),
        ),
        NOW,
    )

    assert len(projection.series) == 1
    assert projection.series[0].key.kind is StatisticKind.ENERGY
    assert projection.series[0].statistics[0].state == Decimal("1.0")


def test_separates_supply_points_and_import_export_directions() -> None:
    start = datetime(2026, 8, 3, 0, tzinfo=UTC)
    projection = project_hourly_statistics(
        (
            _record(start, "0.4", direction=ReadingDirection.IMPORT),
            _record(start, "0.2", direction=ReadingDirection.EXPORT),
            _record(start, "0.7", supply_point_id="SP-2"),
        ),
        NOW,
    )

    assert [
        (series.key.supply_point_id, series.key.direction, series.statistics[0].state)
        for series in projection.series
    ] == [
        ("SP-1", ReadingDirection.EXPORT, Decimal("0.2")),
        ("SP-1", ReadingDirection.IMPORT, Decimal("0.4")),
        ("SP-2", ReadingDirection.IMPORT, Decimal("0.7")),
    ]


def test_ignores_unfinished_interval() -> None:
    projection = project_hourly_statistics(
        (_record(NOW - timedelta(minutes=15), "1", end=NOW + timedelta(minutes=15)),),
        NOW,
    )
    assert projection.series == ()


def test_rejects_naive_projection_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        project_hourly_statistics(
            (),
            datetime(2026, 8, 3, 3, tzinfo=UTC).replace(tzinfo=None),
        )


def test_hourly_statistic_rejects_unaligned_timestamp() -> None:
    with pytest.raises(ValueError, match="aligned"):
        HourlyStatistic(
            datetime(2026, 8, 3, 0, 30, tzinfo=UTC),
            Decimal(1),
            Decimal(1),
        )


def test_hourly_statistic_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        HourlyStatistic(
            datetime(2026, 8, 3, tzinfo=UTC).replace(tzinfo=None),
            Decimal(1),
            Decimal(1),
        )


@given(
    st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("100"),
            places=3,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=8,
    )
)
def test_projection_is_order_independent_and_duplicate_idempotent(
    values: list[Decimal],
) -> None:
    records = tuple(
        _record(
            datetime(2026, 8, 2, tzinfo=UTC) + timedelta(minutes=30 * index),
            str(value),
        )
        for index, value in enumerate(values)
    )

    forward = project_hourly_statistics(records, NOW)
    reversed_projection = project_hourly_statistics(tuple(reversed(records)), NOW)
    duplicated = project_hourly_statistics(records + records, NOW)

    assert reversed_projection == forward
    assert duplicated == forward
