"""The per-kWh additions a supply point was charged, kept because the API forgets them.

`fuelCostAdjustment` and `renewableEnergyLevy` arrive with the period they apply to, and the
provider serves only the one in force. An hour from a month whose adjustment has already been
replaced cannot be priced from the API at any later date — the value is simply gone. Every
other input to a cost can be re-fetched; these two cannot, which is why they are the one thing
this integration keeps a private archive of.

Until an archive has filled, an hour outside every stored window is priced with the nearest
stored value: the earliest for an hour before the archive begins, the latest for an hour after
it ends. Using the newest value for every uncovered hour was considered and rejected — a
Japanese fuel-cost adjustment changes by several yen per kWh and changes sign, so pricing a
two-year-old hour with this month's figure is a confident wrong number of unbounded size.
Reaching to the near end of what is known is the smallest defensible extrapolation, and it
converges on the truth as the archive fills.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from .api.tariff import SupplyPointTariff, TariffAdder

TARIFF_HISTORY_SCHEMA_VERSION: Final = 1


class TariffHistoryError(Exception):
    """A stored adder archive could not be read."""


class AdderKind(StrEnum):
    """Which per-kWh addition an archived record is."""

    FUEL_COST_ADJUSTMENT = "fuel_cost_adjustment"
    RENEWABLE_ENERGY_LEVY = "renewable_energy_levy"


@dataclass(frozen=True, slots=True, order=True)
class ArchivedAdder:
    """One per-kWh addition and the period the provider said it applied to.

    `valid_from` is required. A record with no start satisfies every moment in history, so one
    would silently price the whole archive, and the provider omits the bound on rates it
    considers open-ended rather than to say "always".
    """

    kind: AdderKind
    valid_from: datetime
    valid_to: datetime | None
    price_inc_tax: Decimal
    first_observed_at: datetime
    price_ex_tax: Decimal | None = None
    # How many times the provider restated this window with a different price. Kept because a
    # revised published rate is worth noticing in a cost report, not because anything acts on it.
    revisions: int = 0

    def __post_init__(self) -> None:
        for value in (self.valid_from, self.valid_to, self.first_observed_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("Archived adder timestamps must be timezone-aware")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("An archived adder must cover a period")

    @property
    def window(self) -> tuple[AdderKind, datetime, datetime | None]:
        """Return what identifies this record: its kind and the period it covers."""
        return (self.kind, self.valid_from, self.valid_to)

    def applies_at(self, moment: datetime) -> bool:
        """Report whether this record covers the given moment."""
        if moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to


@dataclass(frozen=True, slots=True)
class AdderRate:
    """The combined per-kWh addition for one moment, and how it was arrived at."""

    total: Decimal
    # True when at least one kind had to be extrapolated from the near end of its archive
    # rather than read from a window covering the moment. Counted in diagnostics so a cost
    # that looks wrong can be attributed.
    extrapolated: bool


@dataclass(frozen=True, slots=True)
class AdderSchedule:
    """Every archived addition for one supply point."""

    records: tuple[ArchivedAdder, ...] = ()

    def rate_at(self, moment: datetime) -> AdderRate:
        """Return what to add per kWh at this moment, summed across kinds."""
        total = Decimal(0)
        extrapolated = False
        for kind in AdderKind:
            of_kind = sorted(record for record in self.records if record.kind is kind)
            if not of_kind:
                continue
            covering = [record for record in of_kind if record.applies_at(moment)]
            if covering:
                # A provider that restates an overlapping window means the later statement.
                total += covering[-1].price_inc_tax
                continue
            nearest = of_kind[0] if moment < of_kind[0].valid_from else of_kind[-1]
            total += nearest.price_inc_tax
            extrapolated = True
        return AdderRate(total=total, extrapolated=extrapolated)


def observed_adders(tariff: SupplyPointTariff) -> tuple[tuple[AdderKind, TariffAdder], ...]:
    """Return the additions a reported tariff carries, in archive terms."""
    pairs = (
        (AdderKind.FUEL_COST_ADJUSTMENT, tariff.fuel_cost_adjustment),
        (AdderKind.RENEWABLE_ENERGY_LEVY, tariff.renewable_energy_levy),
    )
    return tuple((kind, adder) for kind, adder in pairs if adder is not None)


def live_schedule(tariff: SupplyPointTariff, *, observed_at: datetime) -> AdderSchedule:
    """Return a schedule holding only what the provider reports right now.

    What an installation with no archive yet prices from, and what every installation priced
    from before the archive existed — except that an hour outside the reported window now gets
    the nearest known rate instead of nothing.
    """
    return AdderSchedule(
        tuple(
            ArchivedAdder(
                kind=kind,
                valid_from=adder.valid_from,
                valid_to=adder.valid_to,
                price_inc_tax=adder.price_inc_tax,
                first_observed_at=observed_at,
                price_ex_tax=adder.price_ex_tax,
            )
            for kind, adder in observed_adders(tariff)
            if adder.valid_from is not None
        )
    )


def merge_observed(
    records: tuple[ArchivedAdder, ...],
    observed: Iterable[tuple[AdderKind, TariffAdder]],
    *,
    observed_at: datetime,
) -> tuple[ArchivedAdder, ...] | None:
    """Return the archive with what was just observed folded in, or None if unchanged.

    Returning None rather than an equal tuple is what keeps the file from being rewritten every
    refresh: the provider restates the same window until it expires.

    A window already held at a different price is overwritten. The provider's current statement
    about its own period is the authority, and it is also what keeps the live tariff and the
    newest archived record from ever disagreeing at read time.
    """
    by_window = {record.window: record for record in records}
    changed = False
    for kind, adder in observed:
        if adder.valid_from is None:
            # No period identity, so nothing to file it under.
            continue
        candidate = ArchivedAdder(
            kind=kind,
            valid_from=adder.valid_from,
            valid_to=adder.valid_to,
            price_inc_tax=adder.price_inc_tax,
            first_observed_at=observed_at,
            price_ex_tax=adder.price_ex_tax,
        )
        existing = by_window.get(candidate.window)
        if existing is None:
            by_window[candidate.window] = candidate
            changed = True
            continue
        if existing.price_inc_tax == candidate.price_inc_tax:
            continue
        by_window[candidate.window] = replace(
            existing,
            price_inc_tax=candidate.price_inc_tax,
            price_ex_tax=candidate.price_ex_tax,
            revisions=existing.revisions + 1,
        )
        changed = True
    return tuple(sorted(by_window.values())) if changed else None


def with_baseline(
    observed: tuple[ArchivedAdder, ...],
    baseline: tuple[ArchivedAdder, ...],
) -> tuple[ArchivedAdder, ...]:
    """Fill windows the archive has never observed with the shipped baseline.

    An observed window is the provider's own statement about this account and always wins;
    the baseline (`adder_baseline.py`) only ever covers a window the archive has nothing
    for. Unlike `merge_observed`, this never runs twice on the same inputs and so has no
    revision to track — it is recomputed fresh by the caller on every read.
    """
    by_window = {record.window: record for record in baseline}
    by_window.update({record.window: record for record in observed})
    return tuple(sorted(by_window.values()))


def baseline_covered_hours(
    hours: Iterable[datetime],
    schedule: AdderSchedule,
    baseline_generated_at: datetime,
) -> int:
    """Count hours priced only because the shipped baseline covered them.

    A baseline record is unmistakable without adding a field to `ArchivedAdder`: every one
    of them carries the exact same `first_observed_at`, stamped once when the baseline was
    last regenerated, which no observed record — each stamped when its own account was
    actually read — could coincidentally share.
    """
    return sum(
        1
        for hour in hours
        if any(
            record.applies_at(hour) and record.first_observed_at == baseline_generated_at
            for record in schedule.records
        )
    )


def serialize_adders(records: Sequence[ArchivedAdder]) -> dict[str, Any]:
    """Return a deterministic payload for one supply point's archive."""
    return {
        "schema_version": TARIFF_HISTORY_SCHEMA_VERSION,
        "adders": [
            {
                "kind": record.kind.value,
                "valid_from": _iso(record.valid_from),
                "valid_to": _iso(record.valid_to),
                "price_inc_tax": str(record.price_inc_tax),
                "price_ex_tax": None if record.price_ex_tax is None else str(record.price_ex_tax),
                "first_observed_at": _iso(record.first_observed_at),
                "revisions": record.revisions,
            }
            for record in sorted(records)
        ],
    }


def deserialize_adders(payload: Mapping[str, Any]) -> tuple[ArchivedAdder, ...]:
    """Return the archive a payload holds, raising if it cannot be read.

    Strict, unlike the API parsers. A payload that cannot be read is the only copy of values the
    provider will not serve again, so reading part of it and silently discarding the rest would
    lose data with no way to know it happened.
    """
    migrated = migrate_tariff_history_payload(payload)
    raw = migrated.get("adders")
    if not isinstance(raw, list):
        raise TariffHistoryError("Stored adder archive did not contain a list of adders")
    records = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TariffHistoryError("Stored adder archive contained a malformed record")
        try:
            records.append(
                ArchivedAdder(
                    kind=AdderKind(_required(item, "kind")),
                    valid_from=_datetime(_required(item, "valid_from")),
                    valid_to=_optional_datetime(item.get("valid_to")),
                    price_inc_tax=_decimal(_required(item, "price_inc_tax")),
                    first_observed_at=_datetime(_required(item, "first_observed_at")),
                    price_ex_tax=_optional_decimal(item.get("price_ex_tax")),
                    revisions=_count(item.get("revisions")),
                )
            )
        except (TypeError, ValueError, InvalidOperation) as err:
            raise TariffHistoryError("Stored adder archive contained a malformed record") from err
    return tuple(sorted(records))


def migrate_tariff_history_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the payload at the current schema version, raising if it cannot get there.

    There is no older shape to convert yet. This exists from the first version so the version
    gate has one home and the next migration has somewhere to go.
    """
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise TariffHistoryError("Stored adder archive had no schema version")
    if version > TARIFF_HISTORY_SCHEMA_VERSION:
        raise TariffHistoryError(
            f"Stored adder archive is version {version}, newer than {TARIFF_HISTORY_SCHEMA_VERSION}"
        )
    if version != TARIFF_HISTORY_SCHEMA_VERSION:
        raise TariffHistoryError(f"Stored adder archive is version {version}, which is unknown")
    return payload


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required(item: Mapping[str, Any], key: str) -> Any:
    value = item.get(key)
    if value is None:
        raise TariffHistoryError(f"Stored adder archive record was missing {key}")
    return value


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TariffHistoryError("Stored adder archive record had an unreadable timestamp")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _datetime(value)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise TariffHistoryError("Stored adder archive record had an unreadable price")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise TariffHistoryError("Stored adder archive record had an unreadable price")
    return parsed


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


def _count(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TariffHistoryError("Stored adder archive record had an unreadable revision count")
    return value
