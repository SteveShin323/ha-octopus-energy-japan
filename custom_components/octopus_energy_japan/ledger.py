"""Correction-aware, partitioned interval ledger domain model."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from .api import (
    EnergyReading,
    EnergyUnit,
    ReadingDirection,
    ReadingGranularity,
    ReadingQuality,
    ReadingSeriesKey,
    ReadingSource,
)

LEDGER_SCHEMA_VERSION = 1
_PARTITION_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


class LedgerError(Exception):
    """Base error for ledger persistence and reconciliation."""


class LedgerConflictError(LedgerError):
    """A snapshot contains irreconcilable records at the same observation time."""


class LedgerPartitionCorruptError(LedgerError):
    """A persisted monthly partition cannot be trusted."""

    def __init__(self, partition_id: str, reason: str) -> None:
        self.partition_id = partition_id
        super().__init__(f"Ledger partition {partition_id} is corrupt ({reason})")


class LedgerMergeStatus(StrEnum):
    """Outcome for one authoritative interval observation."""

    INSERTED = "inserted"
    CORRECTED = "corrected"
    DELETED = "deleted"
    UNCHANGED = "unchanged"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class LedgerIntervalKey:
    """Logical identity of one provider-series interval."""

    series: ReadingSeriesKey
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("Ledger interval timestamps must be timezone-aware")
        if self.end_at <= self.start_at:
            raise ValueError("Ledger interval end must be later than start")
        object.__setattr__(self, "start_at", self.start_at.astimezone(UTC))
        object.__setattr__(self, "end_at", self.end_at.astimezone(UTC))

    @classmethod
    def from_reading(cls, reading: EnergyReading) -> LedgerIntervalKey:
        """Build a logical key from a normalized reading."""
        return cls(
            ReadingSeriesKey.from_reading(reading),
            reading.start_at,
            reading.end_at,
        )

    @property
    def partition_id(self) -> str:
        """Return the UTC start-month partition identifier."""
        return partition_id_for(self.start_at)


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """Authoritative reading plus local correction metadata."""

    reading: EnergyReading
    correction_count: int = 0

    def __post_init__(self) -> None:
        if self.reading.fetched_at is None:
            raise ValueError("Ledger readings must include fetched_at")
        if self.correction_count < 0:
            raise ValueError("Ledger correction_count must not be negative")

    @property
    def key(self) -> LedgerIntervalKey:
        """Return the logical interval key."""
        return LedgerIntervalKey.from_reading(self.reading)


@dataclass(frozen=True, slots=True)
class LedgerChange:
    """One deterministic ledger merge decision."""

    key: LedgerIntervalKey
    status: LedgerMergeStatus
    previous: LedgerRecord | None = None
    current: LedgerRecord | None = None


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    """Combined reconciliation result used by persistence and statistics."""

    changes: tuple[LedgerChange, ...] = ()
    skipped_corrupt_partitions: tuple[str, ...] = ()

    @property
    def inserted_count(self) -> int:
        return self._count(LedgerMergeStatus.INSERTED)

    @property
    def corrected_count(self) -> int:
        return self._count(LedgerMergeStatus.CORRECTED)

    @property
    def deleted_count(self) -> int:
        return self._count(LedgerMergeStatus.DELETED)

    @property
    def unchanged_count(self) -> int:
        return self._count(LedgerMergeStatus.UNCHANGED)

    @property
    def stale_count(self) -> int:
        return self._count(LedgerMergeStatus.STALE)

    @property
    def changed(self) -> bool:
        return any(
            change.status
            in {
                LedgerMergeStatus.INSERTED,
                LedgerMergeStatus.CORRECTED,
                LedgerMergeStatus.DELETED,
            }
            for change in self.changes
        )

    @property
    def earliest_changed_at(self) -> datetime | None:
        starts = [
            change.key.start_at
            for change in self.changes
            if change.status
            in {
                LedgerMergeStatus.INSERTED,
                LedgerMergeStatus.CORRECTED,
                LedgerMergeStatus.DELETED,
            }
        ]
        return min(starts, default=None)

    @property
    def changed_partitions(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    change.key.partition_id
                    for change in self.changes
                    if change.status
                    in {
                        LedgerMergeStatus.INSERTED,
                        LedgerMergeStatus.CORRECTED,
                        LedgerMergeStatus.DELETED,
                    }
                }
            )
        )

    def _count(self, status: LedgerMergeStatus) -> int:
        return sum(change.status is status for change in self.changes)

    @classmethod
    def combine(cls, results: Iterable[CorrectionResult]) -> CorrectionResult:
        """Combine partition-local results deterministically."""
        changes: list[LedgerChange] = []
        corrupt: set[str] = set()
        for result in results:
            changes.extend(result.changes)
            corrupt.update(result.skipped_corrupt_partitions)
        return cls(
            tuple(sorted(changes, key=_change_sort_key)),
            tuple(sorted(corrupt)),
        )


class LedgerBackend(Protocol):
    """Persistence boundary used by the partitioned ledger."""

    async def async_load_index(self) -> set[str]:
        """Return known monthly partition identifiers."""

    async def async_save_index(self, partitions: set[str]) -> None:
        """Persist known monthly partition identifiers."""

    async def async_load_partition(self, partition_id: str) -> Mapping[str, Any] | None:
        """Load a raw partition payload."""

    async def async_save_partition(
        self,
        partition_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Persist one raw partition payload atomically."""

    async def async_remove_partition(self, partition_id: str) -> None:
        """Remove one partition after explicit cleanup."""


class MemoryLedgerBackend:
    """Deterministic in-memory backend for tests and local services."""

    def __init__(
        self,
        *,
        index: Iterable[str] = (),
        partitions: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.index = set(index)
        self.partitions = {key: dict(value) for key, value in (partitions or {}).items()}
        self.save_counts: dict[str, int] = {}
        self.index_save_count = 0

    async def async_load_index(self) -> set[str]:
        return set(self.index)

    async def async_save_index(self, partitions: set[str]) -> None:
        self.index = set(partitions)
        self.index_save_count += 1

    async def async_load_partition(self, partition_id: str) -> Mapping[str, Any] | None:
        return self.partitions.get(partition_id)

    async def async_save_partition(
        self,
        partition_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.partitions[partition_id] = dict(payload)
        self.save_counts[partition_id] = self.save_counts.get(partition_id, 0) + 1

    async def async_remove_partition(self, partition_id: str) -> None:
        self.partitions.pop(partition_id, None)
        self.index.discard(partition_id)


class IntervalLedger:
    """In-memory authoritative interval records grouped by UTC start month."""

    def __init__(self) -> None:
        self._partitions: dict[str, dict[LedgerIntervalKey, LedgerRecord]] = {}

    @property
    def loaded_partitions(self) -> frozenset[str]:
        return frozenset(self._partitions)

    def load_partition(
        self,
        partition_id: str,
        records: Iterable[LedgerRecord],
    ) -> None:
        """Load one complete partition, rejecting conflicts and misplaced rows."""
        validate_partition_id(partition_id)
        loaded: dict[LedgerIntervalKey, LedgerRecord] = {}
        for record in records:
            if record.key.partition_id != partition_id:
                raise LedgerPartitionCorruptError(partition_id, "record outside partition")
            existing = loaded.get(record.key)
            if existing is not None and existing != record:
                raise LedgerPartitionCorruptError(partition_id, "conflicting duplicate record")
            loaded[record.key] = record
        self._partitions[partition_id] = loaded

    def unload_partition(self, partition_id: str) -> None:
        """Evict one in-memory partition after it has been persisted."""
        self._partitions.pop(partition_id, None)

    def partition_records(self, partition_id: str) -> tuple[LedgerRecord, ...]:
        """Return one loaded partition in stable order."""
        validate_partition_id(partition_id)
        try:
            records = self._partitions[partition_id]
        except KeyError as err:
            raise LedgerError(f"Ledger partition {partition_id} is not loaded") from err
        return tuple(records[key] for key in sorted(records, key=_key_sort_key))

    def records(self) -> tuple[LedgerRecord, ...]:
        """Return every currently loaded record in deterministic order."""
        return tuple(
            record
            for partition_id in sorted(self._partitions)
            for record in self.partition_records(partition_id)
        )

    def reconcile_partition(
        self,
        partition_id: str,
        series: frozenset[ReadingSeriesKey],
        start_at: datetime,
        end_at: datetime,
        readings: Sequence[EnergyReading],
        observed_at: datetime,
    ) -> CorrectionResult:
        """Merge an authoritative snapshot for one partition and time range."""
        validate_partition_id(partition_id)
        start, end = _validated_window(start_at, end_at)
        observed = _required_utc(observed_at, "Ledger observation")
        if partition_id not in self._partitions:
            raise LedgerError(f"Ledger partition {partition_id} is not loaded")
        if not series:
            raise ValueError("Authoritative ledger snapshot requires at least one series")

        incoming: dict[LedgerIntervalKey, EnergyReading] = {}
        for reading in readings:
            _validate_snapshot_reading(
                reading,
                partition_id=partition_id,
                series=series,
                start_at=start,
                end_at=end,
                observed_at=observed,
            )
            key = LedgerIntervalKey.from_reading(reading)
            existing_incoming = incoming.get(key)
            if existing_incoming is not None and existing_incoming != reading:
                raise LedgerConflictError("Snapshot contained a conflicting duplicate interval")
            incoming[key] = reading

        records = self._partitions[partition_id]
        changes: list[LedgerChange] = []
        for key in sorted(incoming, key=_key_sort_key):
            reading = incoming[key]
            previous = records.get(key)
            if previous is None:
                current = LedgerRecord(reading)
                records[key] = current
                changes.append(LedgerChange(key, LedgerMergeStatus.INSERTED, current=current))
                continue

            previous_observed = previous.reading.fetched_at
            assert previous_observed is not None
            if _reading_payload(previous.reading) == _reading_payload(reading):
                changes.append(
                    LedgerChange(
                        key,
                        LedgerMergeStatus.UNCHANGED,
                        previous=previous,
                        current=previous,
                    )
                )
            elif observed < previous_observed:
                changes.append(
                    LedgerChange(
                        key,
                        LedgerMergeStatus.STALE,
                        previous=previous,
                        current=previous,
                    )
                )
            elif observed == previous_observed:
                raise LedgerConflictError(
                    "Conflicting interval values shared the same observation time"
                )
            else:
                current = LedgerRecord(
                    reading,
                    correction_count=previous.correction_count + 1,
                )
                records[key] = current
                changes.append(
                    LedgerChange(
                        key,
                        LedgerMergeStatus.CORRECTED,
                        previous=previous,
                        current=current,
                    )
                )

        for key in sorted(tuple(records), key=_key_sort_key):
            if (
                key in incoming
                or key.series not in series
                or key.start_at < start
                or key.end_at > end
            ):
                continue
            previous = records[key]
            previous_observed = previous.reading.fetched_at
            assert previous_observed is not None
            if observed < previous_observed:
                changes.append(
                    LedgerChange(
                        key,
                        LedgerMergeStatus.STALE,
                        previous=previous,
                        current=previous,
                    )
                )
                continue
            del records[key]
            changes.append(
                LedgerChange(
                    key,
                    LedgerMergeStatus.DELETED,
                    previous=previous,
                )
            )

        return CorrectionResult(tuple(sorted(changes, key=_change_sort_key)))


def expand_authoritative_series(
    queried_series: frozenset[ReadingSeriesKey],
    authoritative_sources: frozenset[ReadingSource],
    existing_records: Iterable[LedgerRecord],
) -> frozenset[ReadingSeriesKey]:
    """Include stored topology variants replaced by a successful provider batch."""
    scopes = {(series.account_id, series.supply_point_id) for series in queried_series}
    if len(scopes) != 1:
        raise ValueError("Authoritative series expansion requires exactly one supply-point scope")
    account_id, supply_point_id = next(iter(scopes))
    expanded = set(queried_series)
    expanded.update(
        record.key.series
        for record in existing_records
        if record.reading.account_id == account_id
        and record.reading.supply_point_id == supply_point_id
        and record.reading.source in authoritative_sources
    )
    return frozenset(expanded)


class PersistentIntervalLedger:
    """Monthly lazy-loading ledger with corruption isolation."""

    def __init__(
        self,
        backend: LedgerBackend,
        *,
        account_id: str,
        supply_point_id: str,
    ) -> None:
        if not account_id or not supply_point_id:
            raise ValueError("Persistent ledger scope identifiers must not be empty")
        self._backend = backend
        self._account_id = account_id
        self._supply_point_id = supply_point_id
        self._ledger = IntervalLedger()
        self._known_partitions: set[str] = set()
        self._corrupt_partitions: set[str] = set()
        self._initialized = False
        self._hot_partitions: frozenset[str] = frozenset()
        self._lock = asyncio.Lock()

    @property
    def ledger(self) -> IntervalLedger:
        return self._ledger

    @property
    def known_partitions(self) -> frozenset[str]:
        return frozenset(self._known_partitions)

    @property
    def corrupt_partitions(self) -> frozenset[str]:
        return frozenset(self._corrupt_partitions)

    async def async_initialize(self, now: datetime) -> None:
        """Load the current and previous UTC partitions and validate the index."""
        async with self._lock:
            current = _required_utc(now, "Ledger initialization")
            known = await self._backend.async_load_index()
            for partition_id in known:
                validate_partition_id(partition_id)
            self._known_partitions = set(known)
            current_partition = partition_id_for(current)
            previous_partition = previous_partition_id(current_partition)
            self._hot_partitions = frozenset({current_partition, previous_partition})
            for partition_id in sorted(self._hot_partitions):
                await self._async_load_partition(partition_id)
            for partition_id in self._ledger.loaded_partitions - self._hot_partitions:
                self._ledger.unload_partition(partition_id)
            self._initialized = True

    async def async_records(
        self,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[LedgerRecord, ...]:
        """Lazy-load and return records whose intervals overlap a UTC window."""
        async with self._lock:
            self._require_initialized()
            start, end = _validated_window(start_at, end_at)
            partitions = _partition_ids_for_overlap_window(start, end)
            loaded_for_request: list[str] = []
            for partition_id in partitions:
                if partition_id not in self._ledger.loaded_partitions:
                    await self._async_load_partition(partition_id)
                    loaded_for_request.append(partition_id)
            records = tuple(
                record
                for partition_id in partitions
                if partition_id not in self._corrupt_partitions
                for record in self._ledger.partition_records(partition_id)
                if record.key.start_at < end and record.key.end_at > start
            )
            self._evict_cold(loaded_for_request)
            return records

    async def async_reconcile(
        self,
        series: frozenset[ReadingSeriesKey],
        start_at: datetime,
        end_at: datetime,
        readings: Sequence[EnergyReading],
        observed_at: datetime,
    ) -> CorrectionResult:
        """Reconcile an authoritative multi-partition API snapshot."""
        async with self._lock:
            self._require_initialized()
            start, end = _validated_window(start_at, end_at)
            observed = _required_utc(observed_at, "Ledger observation")
            if not series:
                raise ValueError("Authoritative ledger snapshot requires at least one series")
            if any(
                key.account_id != self._account_id or key.supply_point_id != self._supply_point_id
                for key in series
            ):
                raise ValueError("Authoritative series does not belong to this ledger scope")
            partitions = partition_ids_for_window(start, end)
            readings_by_partition: dict[str, list[EnergyReading]] = {
                partition_id: [] for partition_id in partitions
            }
            for reading in readings:
                partition_id = partition_id_for(reading.start_at)
                if partition_id not in readings_by_partition:
                    raise ValueError("Snapshot reading started outside the requested window")
                readings_by_partition[partition_id].append(reading)

            loaded_for_request: list[str] = []
            results: list[CorrectionResult] = []
            staged: list[tuple[str, CorrectionResult, tuple[LedgerRecord, ...]]] = []
            try:
                for partition_id in partitions:
                    if partition_id not in self._ledger.loaded_partitions:
                        await self._async_load_partition(partition_id)
                        loaded_for_request.append(partition_id)
                    if partition_id in self._corrupt_partitions:
                        results.append(CorrectionResult(skipped_corrupt_partitions=(partition_id,)))
                        continue

                    candidate = IntervalLedger()
                    candidate.load_partition(
                        partition_id,
                        self._ledger.partition_records(partition_id),
                    )
                    result = candidate.reconcile_partition(
                        partition_id,
                        series,
                        start,
                        end,
                        readings_by_partition[partition_id],
                        observed,
                    )
                    results.append(result)
                    staged.append(
                        (
                            partition_id,
                            result,
                            candidate.partition_records(partition_id),
                        )
                    )

                index_changed = False
                for partition_id, result, records in staged:
                    if not result.changed:
                        continue
                    await self._backend.async_save_partition(
                        partition_id,
                        serialize_partition(partition_id, records),
                    )
                    self._ledger.load_partition(partition_id, records)
                    if partition_id not in self._known_partitions:
                        self._known_partitions.add(partition_id)
                        index_changed = True

                if index_changed:
                    await self._backend.async_save_index(set(self._known_partitions))
                return CorrectionResult.combine(results)
            finally:
                self._evict_cold(loaded_for_request)

    async def async_replace_corrupt_partition(
        self,
        partition_id: str,
        records: Sequence[LedgerRecord],
    ) -> None:
        """Replace one corrupt partition only after an explicit full backfill."""
        async with self._lock:
            self._require_initialized()
            if partition_id not in self._corrupt_partitions:
                raise LedgerError(f"Ledger partition {partition_id} is not marked corrupt")
            self._validate_scope(
                partition_id,
                (record.reading for record in records),
            )
            self._ledger.load_partition(partition_id, records)
            await self._backend.async_save_partition(
                partition_id,
                serialize_partition(partition_id, records),
            )
            self._corrupt_partitions.remove(partition_id)
            if partition_id not in self._known_partitions:
                self._known_partitions.add(partition_id)
                await self._backend.async_save_index(set(self._known_partitions))
            if partition_id not in self._hot_partitions:
                self._ledger.unload_partition(partition_id)

    async def async_remove_partition(self, partition_id: str) -> None:
        """Explicitly remove one partition and update its index atomically."""
        async with self._lock:
            self._require_initialized()
            validate_partition_id(partition_id)
            await self._backend.async_remove_partition(partition_id)
            self._known_partitions.discard(partition_id)
            self._corrupt_partitions.discard(partition_id)
            self._ledger.unload_partition(partition_id)
            await self._backend.async_save_index(set(self._known_partitions))

    async def _async_load_partition(self, partition_id: str) -> None:
        if partition_id in self._ledger.loaded_partitions:
            return
        payload = await self._backend.async_load_partition(partition_id)
        if payload is None:
            if partition_id in self._known_partitions:
                self._corrupt_partitions.add(partition_id)
            self._ledger.load_partition(partition_id, ())
            return
        try:
            records = deserialize_partition(partition_id, payload)
            self._validate_scope(
                partition_id,
                (record.reading for record in records),
            )
            self._ledger.load_partition(partition_id, records)
        except LedgerPartitionCorruptError:
            self._corrupt_partitions.add(partition_id)
            self._ledger.load_partition(partition_id, ())

    def _evict_cold(self, candidates: Iterable[str]) -> None:
        for partition_id in candidates:
            if partition_id not in self._hot_partitions:
                self._ledger.unload_partition(partition_id)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise LedgerError("Persistent ledger has not been initialized")

    def _validate_scope(
        self,
        partition_id: str,
        readings: Iterable[EnergyReading],
    ) -> None:
        if any(
            reading.account_id != self._account_id
            or reading.supply_point_id != self._supply_point_id
            for reading in readings
        ):
            raise LedgerPartitionCorruptError(
                partition_id,
                "record belongs to another supply point",
            )


def serialize_partition(
    partition_id: str,
    records: Iterable[LedgerRecord],
) -> dict[str, Any]:
    """Serialize a partition without losing Decimal or revision metadata."""
    validate_partition_id(partition_id)
    serialized = [_serialize_record(record) for record in records]
    serialized.sort(key=lambda item: _serialized_record_sort_key(item))
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "partition": partition_id,
        "records": serialized,
    }


def deserialize_partition(
    partition_id: str,
    payload: Mapping[str, Any],
) -> tuple[LedgerRecord, ...]:
    """Validate, migrate, and deserialize one persisted partition."""
    validate_partition_id(partition_id)
    migrated = migrate_partition_payload(partition_id, payload)
    raw_records = migrated.get("records")
    if not isinstance(raw_records, list):
        raise LedgerPartitionCorruptError(partition_id, "records is not a list")
    try:
        records = tuple(_deserialize_record(item) for item in raw_records)
    except (KeyError, TypeError, ValueError, InvalidOperation) as err:
        raise LedgerPartitionCorruptError(partition_id, "record validation failed") from err
    validator = IntervalLedger()
    validator.load_partition(partition_id, records)
    return validator.partition_records(partition_id)


def migrate_partition_payload(
    partition_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Migrate supported historical schemas to the current payload."""
    validate_partition_id(partition_id)
    if not isinstance(payload, Mapping):
        raise LedgerPartitionCorruptError(partition_id, "payload is not an object")
    version = payload.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        raise LedgerPartitionCorruptError(partition_id, "schema version is invalid")
    stored_partition = payload.get("partition", partition_id)
    if stored_partition != partition_id:
        raise LedgerPartitionCorruptError(partition_id, "partition identifier mismatch")
    if version > LEDGER_SCHEMA_VERSION:
        raise LedgerPartitionCorruptError(partition_id, "schema is newer than supported")

    migrated = dict(payload)
    if version == 0:
        raw_records = migrated.get("records")
        if not isinstance(raw_records, list):
            raise LedgerPartitionCorruptError(partition_id, "legacy records is not a list")
        try:
            migrated["records"] = [_migrate_v0_record(record) for record in raw_records]
        except (TypeError, ValueError) as err:
            raise LedgerPartitionCorruptError(
                partition_id,
                "legacy record migration failed",
            ) from err
        migrated["schema_version"] = 1
        migrated["partition"] = partition_id
        version = 1
    return migrated


def partition_id_for(value: datetime) -> str:
    """Return a stable UTC start-month partition identifier."""
    utc = _required_utc(value, "Partition timestamp")
    return f"{utc.year:04d}-{utc.month:02d}"


def validate_partition_id(partition_id: str) -> None:
    """Validate a monthly partition identifier without accepting paths."""
    if _PARTITION_PATTERN.fullmatch(partition_id) is None:
        raise ValueError("Ledger partition identifier must use YYYY-MM")


def partition_bounds(partition_id: str) -> tuple[datetime, datetime]:
    """Return one UTC month's half-open bounds."""
    validate_partition_id(partition_id)
    year, month = (int(part) for part in partition_id.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        return start, datetime(year + 1, 1, 1, tzinfo=UTC)
    return start, datetime(year, month + 1, 1, tzinfo=UTC)


def previous_partition_id(partition_id: str) -> str:
    """Return the UTC month before a partition."""
    start, _end = partition_bounds(partition_id)
    if start.month == 1:
        return f"{start.year - 1:04d}-12"
    return f"{start.year:04d}-{start.month - 1:02d}"


def partition_ids_for_window(start_at: datetime, end_at: datetime) -> tuple[str, ...]:
    """Return UTC start-month partitions touched by a half-open window."""
    start, end = _validated_window(start_at, end_at)
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    partitions: list[str] = []
    while cursor < end:
        partitions.append(partition_id_for(cursor))
        _partition_start, cursor = partition_bounds(partitions[-1])
    return tuple(partitions)


def _partition_ids_for_overlap_window(
    start_at: datetime,
    end_at: datetime,
) -> tuple[str, ...]:
    """Include the prior start-month that may own a boundary-crossing interval."""
    partitions = partition_ids_for_window(start_at, end_at)
    prior = previous_partition_id(partitions[0])
    return (prior, *partitions)


def _serialize_record(record: LedgerRecord) -> dict[str, Any]:
    reading = record.reading
    fetched_at = reading.fetched_at
    assert fetched_at is not None
    return {
        "account_id": reading.account_id,
        "supply_point_id": reading.supply_point_id,
        "device_id": reading.device_id,
        "register_id": reading.register_id,
        "direction": reading.direction.value,
        "start_at": _serialize_datetime(reading.start_at),
        "end_at": _serialize_datetime(reading.end_at),
        "value": str(reading.value),
        "unit": reading.unit.value,
        "granularity": (reading.granularity.value if reading.granularity is not None else None),
        "source": reading.source.value,
        "version": reading.version,
        "qualities": [
            {
                "code": quality.code,
                "value": str(quality.value) if quality.value is not None else None,
                "count": quality.count,
                "description": quality.description,
            }
            for quality in reading.qualities
        ],
        "official_cost": (
            str(reading.official_cost) if reading.official_cost is not None else None
        ),
        "fetched_at": _serialize_datetime(fetched_at),
        "correction_count": record.correction_count,
    }


def _deserialize_record(raw: object) -> LedgerRecord:
    if not isinstance(raw, Mapping):
        raise TypeError("Ledger record is not an object")
    qualities_value = raw["qualities"]
    if not isinstance(qualities_value, list):
        raise TypeError("Ledger qualities is not a list")
    qualities = tuple(_deserialize_quality(value) for value in qualities_value)
    fetched_at = _deserialize_datetime(raw["fetched_at"])
    reading = EnergyReading(
        account_id=_required_string(raw["account_id"]),
        supply_point_id=_required_string(raw["supply_point_id"]),
        device_id=_optional_string(raw.get("device_id")),
        register_id=_optional_string(raw.get("register_id")),
        direction=ReadingDirection(_required_string(raw["direction"])),
        start_at=_deserialize_datetime(raw["start_at"]),
        end_at=_deserialize_datetime(raw["end_at"]),
        value=_deserialize_decimal(raw["value"]),
        unit=EnergyUnit(_required_string(raw["unit"])),
        granularity=(
            ReadingGranularity(_required_string(raw["granularity"]))
            if raw.get("granularity") is not None
            else None
        ),
        source=ReadingSource(_required_string(raw["source"])),
        version=_optional_string(raw.get("version")),
        qualities=qualities,
        official_cost=(
            _deserialize_decimal(raw["official_cost"])
            if raw.get("official_cost") is not None
            else None
        ),
        fetched_at=fetched_at,
    )
    correction_count = raw["correction_count"]
    if (
        isinstance(correction_count, bool)
        or not isinstance(correction_count, int)
        or correction_count < 0
    ):
        raise ValueError("Invalid ledger correction count")
    return LedgerRecord(reading, correction_count)


def _deserialize_quality(raw: object) -> ReadingQuality:
    if not isinstance(raw, Mapping):
        raise TypeError("Ledger quality is not an object")
    count = raw.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise ValueError("Invalid ledger quality count")
    return ReadingQuality(
        code=_required_string(raw["code"]),
        value=(_deserialize_decimal(raw["value"]) if raw.get("value") is not None else None),
        count=count,
        description=_optional_string(raw.get("description")),
    )


def _migrate_v0_record(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise TypeError("Legacy ledger record is not an object")
    migrated = dict(raw)
    quality = migrated.pop("quality", None)
    migrated["qualities"] = [] if quality is None else [quality]
    migrated["official_cost"] = migrated.pop("cost_estimate", None)
    migrated.setdefault("device_id", None)
    migrated.setdefault("register_id", None)
    migrated.setdefault("granularity", None)
    migrated.setdefault("correction_count", 0)
    return migrated


def _reading_payload(reading: EnergyReading) -> EnergyReading:
    return replace(reading, fetched_at=None)


def _validate_snapshot_reading(
    reading: EnergyReading,
    *,
    partition_id: str,
    series: frozenset[ReadingSeriesKey],
    start_at: datetime,
    end_at: datetime,
    observed_at: datetime,
) -> None:
    if ReadingSeriesKey.from_reading(reading) not in series:
        raise ValueError("Snapshot contained a reading outside its authoritative series")
    if reading.start_at < start_at or reading.end_at > end_at:
        raise ValueError("Snapshot contained a reading outside its requested window")
    if partition_id_for(reading.start_at) != partition_id:
        raise ValueError("Snapshot reading belongs to a different partition")
    if reading.fetched_at is None:
        raise ValueError("Snapshot readings must include fetched_at")
    if reading.fetched_at.astimezone(UTC) != observed_at:
        raise ValueError("Snapshot readings must share the observation timestamp")


def _serialize_datetime(value: datetime) -> str:
    return _required_utc(value, "Ledger datetime").isoformat().replace("+00:00", "Z")


def _deserialize_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Ledger datetime is not a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Ledger datetime is timezone-naive")
    return parsed.astimezone(UTC)


def _deserialize_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise TypeError("Ledger decimal has an invalid type")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("Ledger decimal is not finite")
    return parsed


def _required_string(value: object) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise ValueError("Ledger string is missing")
    return normalized


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validated_window(start_at: datetime, end_at: datetime) -> tuple[datetime, datetime]:
    start = _required_utc(start_at, "Ledger window start")
    end = _required_utc(end_at, "Ledger window end")
    if end <= start:
        raise ValueError("Ledger window end must be later than start")
    return start, end


def _required_utc(value: datetime, context: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{context} must be timezone-aware")
    return value.astimezone(UTC)


def _key_sort_key(key: LedgerIntervalKey) -> tuple[Any, ...]:
    return (
        key.series.account_id,
        key.series.supply_point_id,
        key.series.device_id or "",
        key.series.register_id or "",
        key.series.direction.value,
        key.series.unit.value,
        key.series.source.value,
        key.start_at,
        key.end_at,
    )


def _change_sort_key(change: LedgerChange) -> tuple[Any, ...]:
    return (*_key_sort_key(change.key), change.status.value)


def _serialized_record_sort_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(record.get(key) or "")
        for key in (
            "account_id",
            "supply_point_id",
            "device_id",
            "register_id",
            "direction",
            "unit",
            "source",
            "start_at",
            "end_at",
        )
    )
