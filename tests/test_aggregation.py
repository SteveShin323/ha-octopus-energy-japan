"""Deterministic source selection and JST calendar aggregation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from custom_components.octopus_energy_japan.aggregation import (
    TOKYO,
    aggregate_calendar,
    aggregate_intervals,
)
from custom_components.octopus_energy_japan.api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingSource,
)
from custom_components.octopus_energy_japan.ledger import (
    LedgerConflictError,
    LedgerRecord,
)
from hypothesis import given
from hypothesis import strategies as st

OBSERVED = datetime(2026, 7, 29, 12, tzinfo=UTC)


def _record(
    local_start: datetime,
    *,
    value: str = "1",
    unit: EnergyUnit = EnergyUnit.KWH,
    source: ReadingSource = ReadingSource.SUPPLY_POINT_READINGS,
    device: str | None = None,
    register: str | None = None,
    cost: str | None = None,
    fetched_at: datetime = OBSERVED,
    correction_count: int = 0,
    account: str = "account-1",
    supply: str = "supply-1",
    direction: ReadingDirection = ReadingDirection.IMPORT,
) -> LedgerRecord:
    start = local_start.astimezone(UTC)
    return LedgerRecord(
        EnergyReading(
            account_id=account,
            supply_point_id=supply,
            device_id=device,
            register_id=register,
            direction=direction,
            start_at=start,
            end_at=start + timedelta(minutes=30),
            value=Decimal(value),
            unit=unit,
            source=source,
            official_cost=Decimal(cost) if cost is not None else None,
            fetched_at=fetched_at,
        ),
        correction_count,
    )


def _jst(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TOKYO)


def test_units_are_normalized_and_distinct_registers_are_summed() -> None:
    records = [
        _record(
            _jst(2026, 7, 1, 0),
            value="500",
            unit=EnergyUnit.WH,
            device="device",
            register="a",
        ),
        _record(
            _jst(2026, 7, 1, 0),
            value="0.001",
            unit=EnergyUnit.MWH,
            device="device",
            register="b",
            cost="20",
        ),
    ]

    interval = aggregate_intervals(records)[0]

    assert interval.energy_kwh == Decimal("1.5")
    assert interval.contributing_records == 2
    assert interval.official_cost is None


@given(
    st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("100"),
            allow_nan=False,
            allow_infinity=False,
            places=6,
        ),
        min_size=1,
        max_size=12,
    )
)
def test_register_aggregation_equals_normalized_interval_sum(
    values: list[Decimal],
) -> None:
    records = [
        _record(
            _jst(2026, 7, 1, 0),
            value=str(value),
            device="device",
            register=f"register-{index}",
        )
        for index, value in enumerate(values)
    ]

    interval = aggregate_intervals(reversed(records))[0]

    assert interval.energy_kwh == sum(values, start=Decimal(0))
    assert interval.contributing_records == len(values)


def test_generic_source_replaces_overlapping_legacy_and_ignores_billing_interval() -> None:
    start = _jst(2026, 7, 1, 0)
    records = [
        _record(
            start,
            value="9",
            source=ReadingSource.LEGACY_HALF_HOURLY,
            cost="90",
        ),
        _record(start, value="1", source=ReadingSource.SUPPLY_POINT_READINGS),
        _record(start, value="100", source=ReadingSource.LEGACY_INTERVAL),
    ]

    interval = aggregate_intervals(records)[0]

    assert interval.energy_kwh == Decimal(1)
    assert interval.official_cost is None


def test_legacy_half_hour_source_is_used_when_generic_is_absent() -> None:
    interval = aggregate_intervals(
        [
            _record(
                _jst(2026, 7, 1, 0),
                value="2",
                source=ReadingSource.LEGACY_HALF_HOURLY,
                cost="30",
            )
        ]
    )[0]
    assert interval.energy_kwh == Decimal(2)
    assert interval.official_cost == Decimal(30)


def test_generic_most_granular_target_prevents_parent_double_counting() -> None:
    start = _jst(2026, 7, 1, 0)
    records = [
        _record(start, value="10"),
        _record(start, value="10", device="device"),
        _record(start, value="4", device="device", register="a"),
        _record(start, value="6", device="device", register="b"),
    ]

    interval = aggregate_intervals(records)[0]

    assert interval.energy_kwh == Decimal(10)
    assert interval.contributing_records == 2


def test_latest_unit_representation_wins_for_same_target() -> None:
    start = _jst(2026, 7, 1, 0)
    older = _record(
        start,
        value="1000",
        unit=EnergyUnit.WH,
        fetched_at=OBSERVED,
    )
    newer = _record(
        start,
        value="1",
        unit=EnergyUnit.KWH,
        fetched_at=OBSERVED + timedelta(hours=1),
        correction_count=2,
    )

    interval = aggregate_intervals([older, newer])[0]

    assert interval.energy_kwh == Decimal(1)
    assert interval.contributing_records == 1
    assert interval.correction_count == 2
    assert aggregate_intervals([newer, older])[0].energy_kwh == Decimal(1)


def test_equivalent_same_observation_units_are_deterministic() -> None:
    start = _jst(2026, 7, 1, 0)
    interval = aggregate_intervals(
        [
            _record(start, value="1000", unit=EnergyUnit.WH),
            _record(start, value="1", unit=EnergyUnit.KWH),
        ]
    )[0]
    assert interval.energy_kwh == Decimal(1)
    assert aggregate_intervals(
        [
            _record(start, value="1", unit=EnergyUnit.KWH),
            _record(start, value="1000", unit=EnergyUnit.WH),
        ]
    )[0].energy_kwh == Decimal(1)

    with pytest.raises(LedgerConflictError, match="Equivalent unit"):
        aggregate_intervals(
            [
                _record(start, value="999", unit=EnergyUnit.WH),
                _record(start, value="1", unit=EnergyUnit.KWH),
            ]
        )


def test_calendar_uses_jst_day_week_and_month_boundaries() -> None:
    now = _jst(2026, 7, 8, 1)
    records = [
        _record(_jst(2026, 7, 8, 0), value="1", cost="10"),
        _record(_jst(2026, 7, 7, 23, 30), value="2", cost="20"),
        _record(_jst(2026, 7, 6, 0), value="3", cost="30"),
        _record(_jst(2026, 7, 5, 23, 30), value="4", cost="40"),
        _record(_jst(2026, 7, 1, 0), value="5", cost="50"),
        _record(_jst(2026, 6, 30, 23, 30), value="6", cost="60"),
        _record(_jst(2026, 6, 1, 0), value="7", cost="70"),
    ]

    snapshot = aggregate_calendar(records, now)
    aggregate = snapshot.supply_points[0]

    assert aggregate.today.energy_kwh == Decimal(1)
    assert aggregate.yesterday.energy_kwh == Decimal(2)
    assert aggregate.this_week.energy_kwh == Decimal(6)
    assert aggregate.this_month.energy_kwh == Decimal(15)
    assert aggregate.last_month.energy_kwh == Decimal(13)
    assert aggregate.this_month.official_cost == Decimal(150)
    assert aggregate.latest_reading_end == _jst(2026, 7, 8, 0, 30).astimezone(UTC)
    assert aggregate.data_delay == timedelta(minutes=30)
    assert snapshot.generated_at == now.astimezone(UTC)
    assert snapshot.timezone == "Asia/Tokyo"


def test_calendar_handles_january_previous_month_boundary() -> None:
    aggregate = aggregate_calendar(
        [_record(_jst(2025, 12, 31, 23, 30), value="2")],
        _jst(2026, 1, 2, 1),
    ).supply_points[0]
    assert aggregate.last_month.energy_kwh == Decimal(2)


def test_period_cost_requires_every_selected_interval_and_future_is_excluded() -> None:
    now = _jst(2026, 7, 8, 1)
    records = [
        _record(_jst(2026, 7, 8, 0), cost="10"),
        _record(_jst(2026, 7, 8, 0, 30), cost=None),
        _record(_jst(2026, 7, 8, 1), value="100", cost="1000"),
    ]

    aggregate = aggregate_calendar(records, now).supply_points[0]

    assert aggregate.today.energy_kwh == Decimal(2)
    assert aggregate.today.official_cost is None
    assert aggregate.today.interval_count == 2


def test_multiple_supply_points_and_directions_are_stably_ordered() -> None:
    now = _jst(2026, 7, 8, 1)
    records = [
        _record(
            _jst(2026, 7, 8, 0),
            account="b",
            supply="z",
            direction=ReadingDirection.EXPORT,
        ),
        _record(_jst(2026, 7, 8, 0), account="a", supply="x"),
    ]

    values = aggregate_calendar(reversed(records), now).supply_points

    assert [(value.account_id, value.supply_point_id) for value in values] == [
        ("a", "x"),
        ("b", "z"),
    ]


def test_empty_and_future_only_snapshots_are_safe() -> None:
    now = _jst(2026, 7, 8, 1)
    assert aggregate_calendar([], now).supply_points == ()
    future = aggregate_calendar(
        [_record(_jst(2026, 7, 8, 2))],
        now,
    )
    assert future.supply_points == ()

    conflicting_future = [
        _record(
            _jst(2026, 7, 8, 2),
            value="1000",
            unit=EnergyUnit.WH,
        ),
        _record(
            _jst(2026, 7, 8, 2),
            value="2",
            unit=EnergyUnit.KWH,
        ),
    ]
    assert aggregate_calendar(conflicting_future, now).supply_points == ()


def test_calendar_rejects_naive_generation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        aggregate_calendar([], datetime(2026, 7, 1))  # noqa: DTZ001
