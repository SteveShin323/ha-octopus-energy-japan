"""Correction, persistence, migration, and recovery tests for the ledger."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import permutations

import pytest
from custom_components.octopus_energy_japan.api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingGranularity,
    ReadingQuality,
    ReadingSeriesKey,
    ReadingSource,
)
from custom_components.octopus_energy_japan.ledger import (
    LEDGER_SCHEMA_VERSION,
    CorrectionResult,
    IntervalLedger,
    LedgerConflictError,
    LedgerError,
    LedgerIntervalKey,
    LedgerPartitionCorruptError,
    LedgerRecord,
    MemoryLedgerBackend,
    PersistentIntervalLedger,
    deserialize_partition,
    expand_authoritative_series,
    migrate_partition_payload,
    partition_bounds,
    partition_id_for,
    partition_ids_for_window,
    previous_partition_id,
    serialize_partition,
    validate_partition_id,
)
from hypothesis import given
from hypothesis import strategies as st

OBSERVED = datetime(2026, 7, 29, 12, tzinfo=UTC)
START = datetime(2026, 7, 1, tzinfo=UTC)


def _reading(
    *,
    start: datetime = START,
    value: str = "1",
    version: str | None = "1",
    fetched_at: datetime = OBSERVED,
    source: ReadingSource = ReadingSource.SUPPLY_POINT_READINGS,
    unit: EnergyUnit = EnergyUnit.KWH,
    supply_point_id: str = "supply-1",
    account_id: str = "account-1",
    direction: ReadingDirection = ReadingDirection.IMPORT,
    device_id: str | None = None,
    register_id: str | None = None,
    official_cost: str | None = None,
) -> EnergyReading:
    return EnergyReading(
        account_id=account_id,
        supply_point_id=supply_point_id,
        device_id=device_id,
        register_id=register_id,
        direction=direction,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        value=Decimal(value),
        unit=unit,
        source=source,
        granularity=ReadingGranularity.THIRTY_MIN,
        version=version,
        qualities=(ReadingQuality("ACTUAL", Decimal(value), 1),),
        official_cost=Decimal(official_cost) if official_cost is not None else None,
        fetched_at=fetched_at,
    )


def _series(reading: EnergyReading) -> frozenset[ReadingSeriesKey]:
    return frozenset({ReadingSeriesKey.from_reading(reading)})


def _loaded_ledger(partition: str = "2026-07") -> IntervalLedger:
    ledger = IntervalLedger()
    ledger.load_partition(partition, ())
    return ledger


def _persistent(
    backend: MemoryLedgerBackend | None = None,
) -> PersistentIntervalLedger:
    return PersistentIntervalLedger(
        backend or MemoryLedgerBackend(),
        account_id="account-1",
        supply_point_id="supply-1",
    )


def test_interval_key_and_partition_normalize_to_utc() -> None:
    reading = _reading(start=datetime(2026, 7, 1, 9, tzinfo=timezone(timedelta(hours=9))))
    record = LedgerRecord(reading)

    assert record.key.start_at == START
    assert record.key.partition_id == "2026-07"
    assert partition_id_for(reading.start_at) == "2026-07"


def test_interval_key_rejects_naive_or_inverted_timestamps() -> None:
    series = ReadingSeriesKey.from_reading(_reading())
    with pytest.raises(ValueError, match="timezone-aware"):
        LedgerIntervalKey(
            series,
            datetime(2026, 7, 1),  # noqa: DTZ001 - invalid-input test
            datetime(2026, 7, 1, 1),  # noqa: DTZ001 - invalid-input test
        )
    with pytest.raises(ValueError, match="later"):
        LedgerIntervalKey(series, START, START)


def test_record_requires_observation_and_valid_correction_count() -> None:
    with pytest.raises(ValueError, match="fetched_at"):
        LedgerRecord(replace(_reading(), fetched_at=None))
    with pytest.raises(ValueError, match="correction_count"):
        LedgerRecord(_reading(), -1)


def test_reconcile_insert_noop_correction_stale_and_delete() -> None:
    ledger = _loaded_ledger()
    original = _reading()

    inserted = ledger.reconcile_partition(
        "2026-07",
        _series(original),
        START,
        START + timedelta(hours=1),
        [original],
        OBSERVED,
    )
    assert inserted.inserted_count == 1
    assert inserted.earliest_changed_at == START
    assert inserted.changed_partitions == ("2026-07",)

    same_payload_later = _reading(fetched_at=OBSERVED + timedelta(hours=1))
    unchanged = ledger.reconcile_partition(
        "2026-07",
        _series(original),
        START,
        START + timedelta(hours=1),
        [same_payload_later],
        OBSERVED + timedelta(hours=1),
    )
    assert unchanged.unchanged_count == 1
    assert ledger.records()[0].reading.fetched_at == OBSERVED

    corrected_reading = _reading(
        value="2",
        version="2",
        fetched_at=OBSERVED + timedelta(hours=2),
    )
    corrected = ledger.reconcile_partition(
        "2026-07",
        _series(original),
        START,
        START + timedelta(hours=1),
        [corrected_reading],
        OBSERVED + timedelta(hours=2),
    )
    assert corrected.corrected_count == 1
    assert ledger.records()[0].correction_count == 1
    assert ledger.records()[0].reading.value == Decimal(2)

    stale = _reading(value="9", fetched_at=OBSERVED + timedelta(minutes=30))
    stale_result = ledger.reconcile_partition(
        "2026-07",
        _series(original),
        START,
        START + timedelta(hours=1),
        [stale],
        OBSERVED + timedelta(minutes=30),
    )
    assert stale_result.stale_count == 1
    assert ledger.records()[0].reading.value == Decimal(2)

    deleted = ledger.reconcile_partition(
        "2026-07",
        _series(original),
        START,
        START + timedelta(hours=1),
        [],
        OBSERVED + timedelta(hours=3),
    )
    assert deleted.deleted_count == 1
    assert ledger.records() == ()


def test_same_observation_conflict_is_rejected() -> None:
    ledger = _loaded_ledger()
    reading = _reading()
    ledger.reconcile_partition(
        "2026-07",
        _series(reading),
        START,
        START + timedelta(hours=1),
        [reading],
        OBSERVED,
    )
    with pytest.raises(LedgerConflictError, match="same observation"):
        ledger.reconcile_partition(
            "2026-07",
            _series(reading),
            START,
            START + timedelta(hours=1),
            [_reading(value="2")],
            OBSERVED,
        )


def test_snapshot_duplicate_order_is_idempotent_and_conflict_is_rejected() -> None:
    first = _reading()
    second = _reading(start=START + timedelta(minutes=30), value="2")
    expected = None
    for order in permutations([first, second]):
        ledger = _loaded_ledger()
        result = ledger.reconcile_partition(
            "2026-07",
            _series(first),
            START,
            START + timedelta(hours=1),
            order,
            OBSERVED,
        )
        state = serialize_partition("2026-07", ledger.records())
        expected = expected or state
        assert state == expected
        assert result.inserted_count == 2

    with pytest.raises(LedgerConflictError, match="duplicate"):
        _loaded_ledger().reconcile_partition(
            "2026-07",
            _series(first),
            START,
            START + timedelta(hours=1),
            [first, _reading(value="2")],
            OBSERVED,
        )


@given(
    st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000"),
            allow_nan=False,
            allow_infinity=False,
            places=6,
        ),
        min_size=1,
        max_size=24,
    )
)
def test_ledger_merge_is_order_independent_and_idempotent(
    values: list[Decimal],
) -> None:
    readings = [
        _reading(
            start=START + timedelta(minutes=30 * index),
            value=str(value),
        )
        for index, value in enumerate(values)
    ]
    end = readings[-1].end_at
    states = []
    for ordered in (readings, list(reversed(readings))):
        ledger = _loaded_ledger()
        first = ledger.reconcile_partition(
            "2026-07",
            _series(readings[0]),
            START,
            end,
            ordered,
            OBSERVED,
        )
        state = serialize_partition("2026-07", ledger.records())
        repeated = ledger.reconcile_partition(
            "2026-07",
            _series(readings[0]),
            START,
            end,
            ordered,
            OBSERVED,
        )
        assert first.inserted_count == len(readings)
        assert repeated.unchanged_count == len(readings)
        assert serialize_partition("2026-07", ledger.records()) == state
        states.append(state)
    assert states[0] == states[1]


def test_deletion_is_scoped_to_series_and_fully_contained_intervals() -> None:
    primary = _reading()
    other = _reading(supply_point_id="supply-2")
    crossing = _reading(start=START - timedelta(minutes=30))
    ledger = _loaded_ledger()
    ledger.load_partition(
        "2026-07",
        [LedgerRecord(primary), LedgerRecord(other)],
    )
    june = _loaded_ledger("2026-06")
    june.load_partition("2026-06", [LedgerRecord(crossing)])

    result = ledger.reconcile_partition(
        "2026-07",
        _series(primary),
        START,
        START + timedelta(hours=1),
        [],
        OBSERVED + timedelta(hours=1),
    )
    assert result.deleted_count == 1
    assert ledger.records() == (LedgerRecord(other),)
    assert june.records() == (LedgerRecord(crossing),)


def test_authoritative_series_expansion_cleans_replaced_topology_and_source() -> None:
    queried = _reading(
        source=ReadingSource.LEGACY_HALF_HOURLY,
    )
    stale_generic = LedgerRecord(
        _reading(
            source=ReadingSource.SUPPLY_POINT_READINGS,
            device_id="retired-device",
        )
    )
    unrelated = LedgerRecord(_reading(supply_point_id="supply-2"))

    expanded = expand_authoritative_series(
        _series(queried),
        frozenset(
            {
                ReadingSource.SUPPLY_POINT_READINGS,
                ReadingSource.LEGACY_HALF_HOURLY,
            }
        ),
        [stale_generic, unrelated],
    )

    assert ReadingSeriesKey.from_reading(stale_generic.reading) in expanded
    assert ReadingSeriesKey.from_reading(unrelated.reading) not in expanded


def test_authoritative_series_expansion_requires_one_scope() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        expand_authoritative_series(frozenset(), frozenset(), ())
    with pytest.raises(ValueError, match="exactly one"):
        expand_authoritative_series(
            _series(_reading()) | _series(_reading(supply_point_id="supply-2")),
            frozenset(),
            (),
        )


def test_stale_authoritative_snapshot_cannot_delete_newer_record() -> None:
    newer = _reading(fetched_at=OBSERVED + timedelta(hours=2))
    ledger = _loaded_ledger()
    ledger.load_partition("2026-07", [LedgerRecord(newer)])

    result = ledger.reconcile_partition(
        "2026-07",
        _series(newer),
        START,
        START + timedelta(hours=1),
        [],
        OBSERVED,
    )

    assert result.stale_count == 1
    assert ledger.records() == (LedgerRecord(newer),)


def test_partition_load_and_reconcile_reject_invalid_state() -> None:
    ledger = IntervalLedger()
    with pytest.raises(LedgerPartitionCorruptError, match="outside"):
        ledger.load_partition(
            "2026-07",
            [LedgerRecord(_reading(start=datetime(2026, 6, 1, tzinfo=UTC)))],
        )

    first = LedgerRecord(_reading())
    conflicting = LedgerRecord(_reading(value="2"))
    with pytest.raises(LedgerPartitionCorruptError, match="duplicate"):
        ledger.load_partition("2026-07", [first, conflicting])

    with pytest.raises(LedgerError, match="not loaded"):
        ledger.partition_records("2026-07")
    with pytest.raises(LedgerError, match="not loaded"):
        ledger.reconcile_partition(
            "2026-07",
            _series(first.reading),
            START,
            START + timedelta(hours=1),
            [],
            OBSERVED,
        )

    ledger.load_partition("2026-07", ())
    with pytest.raises(ValueError, match="at least one"):
        ledger.reconcile_partition(
            "2026-07",
            frozenset(),
            START,
            START + timedelta(hours=1),
            [],
            OBSERVED,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda reading: _reading(start=START - timedelta(minutes=30)),
        lambda reading: _reading(supply_point_id="other"),
        lambda reading: EnergyReading(
            account_id=reading.account_id,
            supply_point_id=reading.supply_point_id,
            direction=reading.direction,
            start_at=reading.start_at,
            end_at=reading.end_at,
            value=reading.value,
            unit=reading.unit,
            source=reading.source,
            fetched_at=None,
        ),
        lambda reading: _reading(fetched_at=OBSERVED + timedelta(seconds=1)),
    ],
)
def test_snapshot_invariants_are_enforced(mutation: object) -> None:
    base = _reading()
    invalid = mutation(base)  # type: ignore[operator]
    with pytest.raises(ValueError):
        _loaded_ledger().reconcile_partition(
            "2026-07",
            _series(base),
            START,
            START + timedelta(hours=1),
            [invalid],
            OBSERVED,
        )


def test_snapshot_partition_invariant_is_enforced() -> None:
    reading = _reading()
    ledger = _loaded_ledger("2026-06")
    with pytest.raises(ValueError, match="different partition"):
        ledger.reconcile_partition(
            "2026-06",
            _series(reading),
            START,
            START + timedelta(hours=1),
            [reading],
            OBSERVED,
        )


def test_partition_serialization_round_trip_preserves_metadata() -> None:
    record = LedgerRecord(
        _reading(
            value="1.250",
            version="revision-x",
            official_cost="44.5",
            device_id="device-1",
            register_id="register-1",
        ),
        correction_count=3,
    )
    payload = serialize_partition("2026-07", [record])

    assert payload["schema_version"] == LEDGER_SCHEMA_VERSION
    assert deserialize_partition("2026-07", payload) == (record,)


def test_v0_migration_maps_quality_and_cost() -> None:
    current = serialize_partition("2026-07", [LedgerRecord(_reading(official_cost="2"))])
    record = current["records"][0]
    record["quality"] = record.pop("qualities")[0]
    record["cost_estimate"] = record.pop("official_cost")
    record.pop("device_id")
    record.pop("register_id")
    record.pop("granularity")
    current.pop("schema_version")

    migrated = migrate_partition_payload("2026-07", current)
    restored = deserialize_partition("2026-07", migrated)

    assert migrated["schema_version"] == 1
    assert restored[0].reading.official_cost == Decimal(2)
    assert restored[0].reading.qualities[0].code == "ACTUAL"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "1", "partition": "2026-07", "records": []},
        {"schema_version": 2, "partition": "2026-07", "records": []},
        {"schema_version": 1, "partition": "2026-08", "records": []},
        {"schema_version": 1, "partition": "2026-07", "records": "bad"},
        {"schema_version": 1, "partition": "2026-07", "records": [{}]},
    ],
)
def test_corrupt_partition_payloads_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(LedgerPartitionCorruptError):
        deserialize_partition("2026-07", payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["records"].append("bad"),
        lambda payload: payload["records"][0].update({"qualities": "bad"}),
        lambda payload: payload["records"][0].update({"correction_count": True}),
        lambda payload: payload["records"][0]["qualities"].append("bad"),
        lambda payload: payload["records"][0]["qualities"][0].update({"count": -1}),
        lambda payload: payload["records"][0].update({"fetched_at": 1}),
        lambda payload: payload["records"][0].update({"fetched_at": "2026-07-29T12:00:00"}),
        lambda payload: payload["records"][0].update({"value": {}}),
        lambda payload: payload["records"][0].update({"value": "NaN"}),
        lambda payload: payload["records"][0].update({"account_id": ""}),
    ],
)
def test_corrupt_record_shapes_are_rejected(mutate: object) -> None:
    payload = serialize_partition("2026-07", [LedgerRecord(_reading())])
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(LedgerPartitionCorruptError):
        deserialize_partition("2026-07", payload)


def test_invalid_legacy_migration_payloads_are_rejected() -> None:
    with pytest.raises(LedgerPartitionCorruptError, match="payload"):
        migrate_partition_payload("2026-07", [])  # type: ignore[arg-type]
    with pytest.raises(LedgerPartitionCorruptError, match="legacy records"):
        migrate_partition_payload("2026-07", {"records": "bad"})
    with pytest.raises(LedgerPartitionCorruptError):
        migrate_partition_payload("2026-07", {"records": ["bad"]})


def test_partition_utilities_cover_year_and_window_boundaries() -> None:
    assert partition_bounds("2026-12") == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert previous_partition_id("2026-01") == "2025-12"
    assert previous_partition_id("2026-07") == "2026-06"
    assert partition_ids_for_window(
        datetime(2026, 6, 30, 23, tzinfo=UTC),
        datetime(2026, 8, 1, tzinfo=UTC),
    ) == ("2026-06", "2026-07")
    with pytest.raises(ValueError):
        validate_partition_id("../2026-07")
    with pytest.raises(ValueError):
        partition_ids_for_window(START, START)
    with pytest.raises(ValueError, match="timezone-aware"):
        partition_id_for(datetime(2026, 7, 1))  # noqa: DTZ001 - invalid-input test


async def test_persistent_ledger_loads_hot_partitions_and_lazy_evicts_old() -> None:
    old_record = LedgerRecord(_reading(start=datetime(2026, 5, 1, tzinfo=UTC), fetched_at=OBSERVED))
    backend = MemoryLedgerBackend(
        index={"2026-05"},
        partitions={"2026-05": serialize_partition("2026-05", [old_record])},
    )
    persistent = _persistent(backend)

    await persistent.async_initialize(OBSERVED)
    assert persistent.ledger.loaded_partitions == frozenset({"2026-06", "2026-07"})

    records = await persistent.async_records(
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert records == (old_record,)
    assert persistent.ledger.loaded_partitions == frozenset({"2026-06", "2026-07"})


async def test_reinitialization_rolls_hot_partitions_without_leaking_memory() -> None:
    persistent = _persistent()
    await persistent.async_initialize(OBSERVED)
    assert persistent.ledger.loaded_partitions == frozenset({"2026-06", "2026-07"})

    await persistent.async_initialize(datetime(2026, 8, 1, tzinfo=UTC))

    assert persistent.ledger.loaded_partitions == frozenset({"2026-07", "2026-08"})


async def test_persistent_records_include_previous_month_boundary_interval() -> None:
    crossing = LedgerRecord(
        _reading(
            start=datetime(2026, 6, 30, 23, 45, tzinfo=UTC),
            fetched_at=OBSERVED,
        )
    )
    backend = MemoryLedgerBackend(
        index={"2026-06"},
        partitions={"2026-06": serialize_partition("2026-06", [crossing])},
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(datetime(2026, 8, 1, tzinfo=UTC))

    records = await persistent.async_records(
        datetime(2026, 7, 1, tzinfo=UTC),
        datetime(2026, 7, 2, tzinfo=UTC),
    )

    assert records == (crossing,)
    assert persistent.ledger.loaded_partitions == frozenset({"2026-07", "2026-08"})


async def test_persistent_reconcile_saves_changed_partitions_and_index_once() -> None:
    backend = MemoryLedgerBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    reading = _reading()

    result = await persistent.async_reconcile(
        _series(reading),
        START,
        START + timedelta(hours=1),
        [reading],
        OBSERVED,
    )
    second = await persistent.async_reconcile(
        _series(reading),
        START,
        START + timedelta(hours=1),
        [_reading(fetched_at=OBSERVED + timedelta(hours=1))],
        OBSERVED + timedelta(hours=1),
    )

    assert result.inserted_count == 1
    assert second.unchanged_count == 1
    assert backend.save_counts == {"2026-07": 1}
    assert backend.index == {"2026-07"}
    assert backend.index_save_count == 1


async def test_persistent_reconcile_updates_known_partition_without_index_write() -> None:
    backend = MemoryLedgerBackend(
        index={"2026-07"},
        partitions={"2026-07": serialize_partition("2026-07", ())},
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    reading = _reading()

    result = await persistent.async_reconcile(
        _series(reading),
        START,
        START + timedelta(hours=1),
        [reading],
        OBSERVED,
    )

    assert result.inserted_count == 1
    assert backend.save_counts == {"2026-07": 1}
    assert backend.index_save_count == 0


async def test_persistent_reconcile_lazy_loads_and_evicts_cold_partition() -> None:
    backend = MemoryLedgerBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    may_start = datetime(2026, 5, 1, tzinfo=UTC)
    reading = _reading(start=may_start)

    result = await persistent.async_reconcile(
        _series(reading),
        may_start,
        may_start + timedelta(hours=1),
        [reading],
        OBSERVED,
    )

    assert result.inserted_count == 1
    assert backend.index == {"2026-05"}
    assert "2026-05" not in persistent.ledger.loaded_partitions
    assert persistent.known_partitions == frozenset({"2026-05"})


async def test_persistent_reconcile_accepts_interval_crossing_utc_month() -> None:
    backend = MemoryLedgerBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    crossing = _reading(start=datetime(2026, 7, 31, 23, 45, tzinfo=UTC))

    result = await persistent.async_reconcile(
        _series(crossing),
        datetime(2026, 7, 31, 23, tzinfo=UTC),
        datetime(2026, 8, 1, 1, tzinfo=UTC),
        [crossing],
        OBSERVED,
    )

    assert result.inserted_count == 1
    assert deserialize_partition(
        "2026-07",
        backend.partitions["2026-07"],
    ) == (LedgerRecord(crossing),)


async def test_multi_partition_validation_is_atomic() -> None:
    observed = datetime(2026, 8, 2, tzinfo=UTC)
    july = _reading(
        start=datetime(2026, 7, 31, 23, tzinfo=UTC),
        fetched_at=observed,
    )
    august_existing = _reading(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        fetched_at=observed,
    )
    august_conflict = replace(august_existing, value=Decimal(2))
    backend = MemoryLedgerBackend(
        index={"2026-07", "2026-08"},
        partitions={
            "2026-07": serialize_partition("2026-07", ()),
            "2026-08": serialize_partition(
                "2026-08",
                [LedgerRecord(august_existing)],
            ),
        },
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(datetime(2026, 8, 15, tzinfo=UTC))

    with pytest.raises(LedgerConflictError, match="same observation"):
        await persistent.async_reconcile(
            _series(july),
            datetime(2026, 7, 31, 23, tzinfo=UTC),
            datetime(2026, 8, 1, 1, tzinfo=UTC),
            [july, august_conflict],
            observed,
        )

    assert (
        deserialize_partition(
            "2026-07",
            backend.partitions["2026-07"],
        )
        == ()
    )
    assert persistent.ledger.partition_records("2026-07") == ()
    assert backend.save_counts == {}


async def test_persistent_reconcile_rejects_reading_outside_partition_plan() -> None:
    persistent = _persistent()
    await persistent.async_initialize(OBSERVED)
    with pytest.raises(ValueError, match="outside"):
        await persistent.async_reconcile(
            _series(_reading()),
            START,
            START + timedelta(hours=1),
            [_reading(start=datetime(2026, 8, 1, tzinfo=UTC))],
            OBSERVED,
        )


async def test_persistent_corruption_is_isolated_and_explicitly_repaired() -> None:
    backend = MemoryLedgerBackend(
        index={"2026-06", "2026-07"},
        partitions={
            "2026-06": {"schema_version": 1, "partition": "2026-06", "records": "bad"},
            "2026-07": serialize_partition("2026-07", ()),
        },
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    june_reading = _reading(start=datetime(2026, 6, 1, tzinfo=UTC))

    result = await persistent.async_reconcile(
        _series(june_reading),
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 2, tzinfo=UTC),
        [june_reading],
        OBSERVED,
    )
    assert result.skipped_corrupt_partitions == ("2026-06",)
    assert persistent.corrupt_partitions == frozenset({"2026-06"})
    assert (
        await persistent.async_records(
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 2, tzinfo=UTC),
        )
        == ()
    )

    await persistent.async_replace_corrupt_partition(
        "2026-06",
        [LedgerRecord(june_reading)],
    )
    assert persistent.corrupt_partitions == frozenset()
    assert deserialize_partition("2026-06", backend.partitions["2026-06"]) == (
        LedgerRecord(june_reading),
    )


async def test_missing_known_partition_is_corrupt_and_removal_updates_index() -> None:
    backend = MemoryLedgerBackend(index={"2026-07"})
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    assert persistent.corrupt_partitions == frozenset({"2026-07"})

    await persistent.async_remove_partition("2026-07")
    assert persistent.corrupt_partitions == frozenset()
    assert backend.index == set()


async def test_cold_unknown_corrupt_partition_can_be_repaired_and_evicted() -> None:
    backend = MemoryLedgerBackend(
        partitions={
            "2026-05": {
                "schema_version": 1,
                "partition": "2026-05",
                "records": "bad",
            }
        }
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    may_start = datetime(2026, 5, 1, tzinfo=UTC)

    assert (
        await persistent.async_records(
            may_start,
            datetime(2026, 6, 1, tzinfo=UTC),
        )
        == ()
    )
    assert persistent.corrupt_partitions == frozenset({"2026-05"})

    record = LedgerRecord(_reading(start=may_start))
    await persistent.async_replace_corrupt_partition("2026-05", [record])
    assert backend.index == {"2026-05"}
    assert "2026-05" not in persistent.ledger.loaded_partitions


async def test_persistent_ledger_requires_initialization_and_valid_repair() -> None:
    persistent = _persistent()
    with pytest.raises(LedgerError, match="initialized"):
        await persistent.async_records(START, START + timedelta(hours=1))

    await persistent.async_initialize(OBSERVED)
    await persistent.async_initialize(OBSERVED)
    with pytest.raises(LedgerError, match="not marked corrupt"):
        await persistent.async_replace_corrupt_partition("2026-07", [])


def test_persistent_ledger_requires_nonempty_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        PersistentIntervalLedger(
            MemoryLedgerBackend(),
            account_id="",
            supply_point_id="supply-1",
        )
    with pytest.raises(ValueError, match="scope"):
        PersistentIntervalLedger(
            MemoryLedgerBackend(),
            account_id="account-1",
            supply_point_id="",
        )


async def test_persistent_ledger_rejects_foreign_authoritative_series() -> None:
    persistent = _persistent()
    await persistent.async_initialize(OBSERVED)
    foreign = _reading(supply_point_id="supply-2")

    with pytest.raises(ValueError, match="at least one"):
        await persistent.async_reconcile(
            frozenset(),
            START,
            START + timedelta(hours=1),
            [],
            OBSERVED,
        )
    with pytest.raises(ValueError, match="ledger scope"):
        await persistent.async_reconcile(
            _series(foreign),
            START,
            START + timedelta(hours=1),
            [foreign],
            OBSERVED,
        )


async def test_persistent_ledger_isolates_foreign_persisted_records() -> None:
    foreign = LedgerRecord(_reading(supply_point_id="supply-2"))
    backend = MemoryLedgerBackend(
        index={"2026-07"},
        partitions={
            "2026-07": serialize_partition("2026-07", [foreign]),
        },
    )
    persistent = _persistent(backend)

    await persistent.async_initialize(OBSERVED)

    assert persistent.corrupt_partitions == frozenset({"2026-07"})
    assert await persistent.async_records(START, START + timedelta(hours=1)) == ()


async def test_persistent_ledger_rejects_foreign_repair_records() -> None:
    backend = MemoryLedgerBackend(
        index={"2026-07"},
        partitions={
            "2026-07": {
                "schema_version": 1,
                "partition": "2026-07",
                "records": "bad",
            },
        },
    )
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)

    with pytest.raises(LedgerPartitionCorruptError, match="another supply point"):
        await persistent.async_replace_corrupt_partition(
            "2026-07",
            [LedgerRecord(_reading(supply_point_id="supply-2"))],
        )


async def test_hot_partition_is_not_evicted_when_reloaded_on_demand() -> None:
    persistent = _persistent()
    await persistent.async_initialize(OBSERVED)
    persistent.ledger.unload_partition("2026-07")

    assert (
        await persistent.async_records(
            START,
            START + timedelta(hours=1),
        )
        == ()
    )
    assert "2026-07" in persistent.ledger.loaded_partitions


async def test_concurrent_reconciliation_is_serialized() -> None:
    class YieldingBackend(MemoryLedgerBackend):
        active_saves = 0
        maximum_active_saves = 0

        async def async_save_partition(
            self,
            partition_id: str,
            payload: dict[str, object],
        ) -> None:
            self.active_saves += 1
            self.maximum_active_saves = max(
                self.maximum_active_saves,
                self.active_saves,
            )
            await asyncio.sleep(0)
            await super().async_save_partition(partition_id, payload)
            self.active_saves -= 1

    backend = YieldingBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    first = _reading()
    second = _reading(
        value="2",
        version="2",
        fetched_at=OBSERVED + timedelta(hours=1),
    )

    await asyncio.gather(
        persistent.async_reconcile(
            _series(first),
            START,
            START + timedelta(hours=1),
            [first],
            OBSERVED,
        ),
        persistent.async_reconcile(
            _series(second),
            START,
            START + timedelta(hours=1),
            [second],
            OBSERVED + timedelta(hours=1),
        ),
    )

    assert backend.maximum_active_saves == 1
    assert persistent.ledger.records()[0].reading.value == Decimal(2)


def test_correction_result_combines_counts_and_corruption() -> None:
    reading = _reading()
    ledger = _loaded_ledger()
    inserted = ledger.reconcile_partition(
        "2026-07",
        _series(reading),
        START,
        START + timedelta(hours=1),
        [reading],
        OBSERVED,
    )
    combined = CorrectionResult.combine(
        [
            inserted,
            CorrectionResult(skipped_corrupt_partitions=("2026-06",)),
            CorrectionResult(skipped_corrupt_partitions=("2026-06",)),
        ]
    )
    assert combined.inserted_count == 1
    assert combined.skipped_corrupt_partitions == ("2026-06",)


async def test_a_partition_the_snapshot_returned_nothing_for_keeps_its_records() -> None:
    """A window spanning several months is split per partition, and one can come back empty.

    That happens whenever the response stopped short of it — a page that ended early, a
    transient provider error, a partial read — and it is indistinguishable from the provider
    withdrawing the whole month. Reading it as a withdrawal deleted 35 days of a real
    account's history on 2026-08-13, while the provider still served every reading when asked
    again. Absence is not evidence.
    """
    backend = MemoryLedgerBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    july = _reading(start=START)
    august = _reading(start=datetime(2026, 8, 1, tzinfo=UTC), fetched_at=OBSERVED)
    window_start = START
    window_end = datetime(2026, 8, 2, tzinfo=UTC)

    await persistent.async_reconcile(
        _series(july) | _series(august),
        window_start,
        window_end,
        [july, august],
        OBSERVED,
    )
    # The same window again, but this time only August came back.
    later = OBSERVED + timedelta(hours=1)
    result = await persistent.async_reconcile(
        _series(july) | _series(august),
        window_start,
        window_end,
        [_reading(start=datetime(2026, 8, 1, tzinfo=UTC), fetched_at=later)],
        later,
    )

    assert result.deleted_count == 0
    assert result.skipped_empty_partitions == ("2026-07",)
    records = await persistent.async_records(window_start, window_end)
    assert [record.reading.start_at for record in records] == [
        july.start_at,
        august.start_at,
    ]


async def test_a_reading_the_provider_replaced_within_a_returned_month_is_still_removed() -> None:
    """The withdrawal rule still applies where the provider actually answered.

    Only a partition that returned nothing at all is left alone. One that returned some
    readings answered authoritatively, so an interval missing from it is a real withdrawal.
    """
    backend = MemoryLedgerBackend()
    persistent = _persistent(backend)
    await persistent.async_initialize(OBSERVED)
    first = _reading(start=START)
    second = _reading(start=START + timedelta(minutes=30))
    window_end = START + timedelta(hours=1)

    await persistent.async_reconcile(
        _series(first),
        START,
        window_end,
        [first, second],
        OBSERVED,
    )
    later = OBSERVED + timedelta(hours=1)
    result = await persistent.async_reconcile(
        _series(first),
        START,
        window_end,
        [_reading(start=START, fetched_at=later)],
        later,
    )

    assert result.deleted_count == 1
    assert result.skipped_empty_partitions == ()
    records = await persistent.async_records(START, window_end)
    assert [record.reading.start_at for record in records] == [first.start_at]
