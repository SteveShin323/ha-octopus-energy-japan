"""The shipped, repo-maintained baseline for the two per-kWh adders.

`tariff_history.py` archives what one installation has actually observed from its own
account, starting from whenever that account was connected. Everything before that, and
every supply point that has never been connected at all, has nothing to price from but the
nearest observed value — a guess that can be off by several yen once it reaches back more
than a month or two.

Both adders are public record, independent of any one customer's account:

- `再エネ賦課金` (the renewable energy levy) is a single nationwide rate the government sets
  once a year and every retailer charges identically.
- `燃料費調整額` (the fuel cost adjustment) is published monthly, per grid area, by the
  provider itself at https://octopusenergy.co.jp/terms — one PDF per month, covering all
  nine areas in a fixed table order. See `docs/ADDER_BASELINE.md` for how that table was
  read and how to refresh it.

So this repository ships one copy of that public record — `data/adder_baseline.json` — and
every installation reads it instead of extrapolating on its own. It is folded in only where an
installation's own archive has nothing (`tariff_history.with_baseline`); an observed record is
always the provider's own statement about itself and is never overridden.

One documented exception: some products (シンプルオクトパス, しかたこシンプル as of this
writing) are billed with no fuel cost adjustment at all, and the live tariff reports this by
returning no `fuelCostAdjustment` rather than a zero one. The baseline has no way to know a
supply point is on such a product before the account has been read at least once, so the
caller (`adders_for` in `__init__.py`) is the one that suppresses the fuel-adjustment baseline
once the live tariff has confirmed the product has none — see `baseline_schedule`'s
`include_fuel_cost_adjustment` parameter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, NamedTuple

from .tariff_history import AdderKind, AdderSchedule, ArchivedAdder

ADDER_BASELINE_SCHEMA_VERSION: Final = 1

_DATA_PATH: Final = Path(__file__).resolve().parent / "data" / "adder_baseline.json"


class AdderBaselineError(Exception):
    """The shipped baseline file could not be read."""


def baseline_generated_at() -> datetime:
    """Return the timestamp every shipped baseline record carries as `first_observed_at`.

    No observed record can coincidentally share this exact value, which is what lets
    `tariff_history.baseline_covered_hours` tell a baseline-sourced hour from an
    observed one without adding a field to `ArchivedAdder` itself.
    """
    return _load().generated_at


def baseline_schedule(
    grid_operator_code: str | None,
    *,
    include_fuel_cost_adjustment: bool = True,
) -> AdderSchedule:
    """Return the shipped baseline for one supply point.

    The renewable energy levy applies everywhere, so it is included even before the grid
    area is known (`grid_operator_code is None`, before the first commercial refresh). The
    fuel cost adjustment needs an area and can be suppressed once the live tariff has shown
    the product carries none at all.
    """
    baseline = _load()
    records: tuple[ArchivedAdder, ...] = baseline.levy
    if include_fuel_cost_adjustment and grid_operator_code is not None:
        records += baseline.fuel_by_area.get(grid_operator_code, ())
    return AdderSchedule(records)


class _Baseline(NamedTuple):
    levy: tuple[ArchivedAdder, ...]
    fuel_by_area: Mapping[str, tuple[ArchivedAdder, ...]]
    generated_at: datetime


@lru_cache(maxsize=1)
def _load() -> _Baseline:
    try:
        raw = _DATA_PATH.read_text(encoding="utf-8")
    except OSError as err:
        raise AdderBaselineError(f"Could not read {_DATA_PATH}") from err
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise AdderBaselineError("Shipped adder baseline is not valid JSON") from err
    return _parse(payload)


def _parse(payload: Any) -> _Baseline:
    if not isinstance(payload, Mapping):
        raise AdderBaselineError("Shipped adder baseline is not an object")
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise AdderBaselineError("Shipped adder baseline had no schema version")
    if version != ADDER_BASELINE_SCHEMA_VERSION:
        raise AdderBaselineError(f"Shipped adder baseline is version {version}, which is unknown")
    generated_at = _datetime(payload.get("fetched_at"))

    levy_raw = payload.get("renewable_energy_levy")
    if not isinstance(levy_raw, list):
        raise AdderBaselineError("Shipped adder baseline's levy table is not a list")
    levy = tuple(
        sorted(_record(AdderKind.RENEWABLE_ENERGY_LEVY, item, generated_at) for item in levy_raw)
    )

    fuel_raw = payload.get("fuel_cost_adjustment")
    if not isinstance(fuel_raw, Mapping):
        raise AdderBaselineError("Shipped adder baseline's fuel table is not an object")
    fuel_by_area: dict[str, tuple[ArchivedAdder, ...]] = {}
    for area, area_records in fuel_raw.items():
        if not isinstance(area_records, list):
            raise AdderBaselineError(
                f"Shipped adder baseline's fuel table for {area} is not a list"
            )
        fuel_by_area[area] = tuple(
            sorted(
                _record(AdderKind.FUEL_COST_ADJUSTMENT, item, generated_at) for item in area_records
            )
        )
    return _Baseline(levy=levy, fuel_by_area=fuel_by_area, generated_at=generated_at)


def _record(kind: AdderKind, item: Any, generated_at: datetime) -> ArchivedAdder:
    if not isinstance(item, Mapping):
        raise AdderBaselineError("Shipped adder baseline contained a malformed record")
    try:
        return ArchivedAdder(
            kind=kind,
            valid_from=_datetime(item.get("valid_from")),
            valid_to=_datetime(item.get("valid_to")),
            price_inc_tax=_decimal(item.get("price_inc_tax")),
            first_observed_at=generated_at,
        )
    except (TypeError, ValueError, InvalidOperation) as err:
        raise AdderBaselineError("Shipped adder baseline contained a malformed record") from err


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AdderBaselineError("Shipped adder baseline had an unreadable timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise AdderBaselineError("Shipped adder baseline had an unreadable timestamp") from err
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise AdderBaselineError("Shipped adder baseline had an unreadable price")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise AdderBaselineError("Shipped adder baseline had an unreadable price")
    return parsed
