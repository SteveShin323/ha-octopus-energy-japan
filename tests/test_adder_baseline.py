"""Tests for the shipped, repo-maintained adder baseline.

Like `test_tou.py`, this checks the transcription itself as much as the code that reads it:
a baseline with a silent gap or an overlapping window produces a confident wrong price rather
than a visible failure, so completeness and consistency are asserted here, not just parsing.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from custom_components.octopus_energy_japan.adder_baseline import (
    _DATA_PATH,
    ADDER_BASELINE_SCHEMA_VERSION,
    AdderBaselineError,
    _load,
    _parse,
    baseline_generated_at,
    baseline_schedule,
)
from custom_components.octopus_energy_japan.tariff_history import AdderKind, ArchivedAdder

AREA_CODES = ("01", "02", "03", "04", "05", "06", "07", "08", "09")


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": ADDER_BASELINE_SCHEMA_VERSION,
        "fetched_at": "2026-01-01T00:00:00Z",
        "renewable_energy_levy": [
            {
                "valid_from": "2026-04-30T15:00:00Z",
                "valid_to": "2027-04-30T15:00:00Z",
                "price_inc_tax": "4.18",
            }
        ],
        "fuel_cost_adjustment": {
            "01": [
                {
                    "valid_from": "2025-12-31T15:00:00Z",
                    "valid_to": "2026-01-31T15:00:00Z",
                    "price_inc_tax": "1.79",
                }
            ]
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not a mapping", id="not-a-mapping"),
        pytest.param({}, id="missing-schema-version"),
        pytest.param({**_valid_payload(), "schema_version": True}, id="bool-schema-version"),
        pytest.param({**_valid_payload(), "schema_version": 999}, id="unknown-schema-version"),
        pytest.param({**_valid_payload(), "fetched_at": 12345}, id="non-string-timestamp"),
        pytest.param(
            {**_valid_payload(), "fetched_at": "not-a-timestamp"}, id="unparseable-timestamp"
        ),
        pytest.param(
            {**_valid_payload(), "renewable_energy_levy": "not-a-list"}, id="levy-not-a-list"
        ),
        pytest.param(
            {**_valid_payload(), "fuel_cost_adjustment": "not-a-mapping"}, id="fuel-not-a-mapping"
        ),
        pytest.param(
            {**_valid_payload(), "fuel_cost_adjustment": {"01": "not-a-list"}},
            id="fuel-area-not-a-list",
        ),
        pytest.param(
            {**_valid_payload(), "renewable_energy_levy": ["not-a-mapping"]},
            id="record-not-a-mapping",
        ),
        pytest.param(
            {
                **_valid_payload(),
                "renewable_energy_levy": [
                    {"valid_from": "2026-04-30T15:00:00Z", "valid_to": "x", "price_inc_tax": "4.18"}
                ],
            },
            id="record-bad-timestamp",
        ),
        pytest.param(
            {
                **_valid_payload(),
                "renewable_energy_levy": [
                    {
                        "valid_from": "2026-04-30T15:00:00Z",
                        "valid_to": "2027-04-30T15:00:00Z",
                        "price_inc_tax": "not-a-number",
                    }
                ],
            },
            id="record-bad-price",
        ),
    ],
)
def test_a_malformed_shipped_file_is_reported_rather_than_silently_mispriced(
    payload: object,
) -> None:
    with pytest.raises(AdderBaselineError):
        _parse(payload)


def test_a_well_formed_payload_parses_without_error() -> None:
    _parse(_valid_payload())  # does not raise


def test_load_reports_a_file_that_cannot_be_read(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    _load.cache_clear()
    try:
        with (
            patch("custom_components.octopus_energy_japan.adder_baseline._DATA_PATH", missing),
            pytest.raises(AdderBaselineError, match="Could not read"),
        ):
            _load()
    finally:
        _load.cache_clear()


def test_load_reports_a_file_that_is_not_valid_json(tmp_path: Path) -> None:
    broken = tmp_path / "adder_baseline.json"
    broken.write_text("{not valid json", encoding="utf-8")
    _load.cache_clear()
    try:
        with (
            patch("custom_components.octopus_energy_japan.adder_baseline._DATA_PATH", broken),
            pytest.raises(AdderBaselineError, match="not valid JSON"),
        ):
            _load()
    finally:
        _load.cache_clear()


def test_the_shipped_file_declares_the_schema_this_module_reads() -> None:
    payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))

    assert payload["schema_version"] == ADDER_BASELINE_SCHEMA_VERSION


def test_every_grid_area_has_a_fuel_cost_adjustment_table() -> None:
    baseline = _load()

    assert set(baseline.fuel_by_area) == set(AREA_CODES)
    for area in AREA_CODES:
        assert len(baseline.fuel_by_area[area]) > 0


def test_the_renewable_energy_levy_has_at_least_one_fiscal_year() -> None:
    baseline = _load()

    assert len(baseline.levy) > 0
    assert all(record.kind is AdderKind.RENEWABLE_ENERGY_LEVY for record in baseline.levy)


def test_no_kind_has_an_overlapping_or_duplicate_window() -> None:
    baseline = _load()

    for area in AREA_CODES:
        _assert_no_overlap(baseline.fuel_by_area[area])
    _assert_no_overlap(baseline.levy)


def _assert_no_overlap(records: Sequence[ArchivedAdder]) -> None:
    ordered = sorted(records, key=lambda r: r.valid_from)
    for earlier, later in itertools.pairwise(ordered):
        assert earlier.valid_to is not None
        assert earlier.valid_to <= later.valid_from, (earlier, later)


def test_every_price_is_a_finite_positive_decimal() -> None:
    baseline = _load()

    for record in (
        *baseline.levy,
        *(r for area in AREA_CODES for r in baseline.fuel_by_area[area]),
    ):
        assert record.price_inc_tax.is_finite()
        assert record.price_inc_tax > 0


def test_baseline_schedule_includes_the_levy_even_with_no_grid_area() -> None:
    schedule = baseline_schedule(None)

    assert all(record.kind is AdderKind.RENEWABLE_ENERGY_LEVY for record in schedule.records)
    assert len(schedule.records) > 0


def test_baseline_schedule_adds_the_area_s_fuel_cost_adjustment() -> None:
    schedule = baseline_schedule("03")

    kinds = {record.kind for record in schedule.records}
    assert kinds == {AdderKind.RENEWABLE_ENERGY_LEVY, AdderKind.FUEL_COST_ADJUSTMENT}


def test_baseline_schedule_can_withhold_the_fuel_cost_adjustment() -> None:
    """Some products (e.g. シンプルオクトパス) are billed with no fuel cost adjustment at all."""
    schedule = baseline_schedule("03", include_fuel_cost_adjustment=False)

    assert all(record.kind is AdderKind.RENEWABLE_ENERGY_LEVY for record in schedule.records)


def test_baseline_schedule_is_empty_for_an_unknown_area() -> None:
    schedule = baseline_schedule("99")

    assert all(record.kind is AdderKind.RENEWABLE_ENERGY_LEVY for record in schedule.records)


def test_every_baseline_record_is_stamped_with_the_same_generation_time() -> None:
    generated_at = baseline_generated_at()
    schedule = baseline_schedule("03")

    assert generated_at.tzinfo is UTC or generated_at.utcoffset() is not None
    assert all(record.first_observed_at == generated_at for record in schedule.records)


def test_no_area_has_an_implausible_month_over_month_jump() -> None:
    """A coarse plausibility bound, not a scramble detector.

    Every month's nine values are extracted in fixed table-row order (01-09) because most of
    the source PDFs never label a row with its area name in machine-readable text — see
    `docs/ADDER_BASELINE.md`. `scripts/refresh_adder_baseline.py` checks a row's label against
    the expected order when the newer PDF layout provides one, but an older, unlabelled month
    still relies on position alone, and a scrambled row there would still pass every other
    test here (no overlap, still a positive finite Decimal) while quietly pricing the wrong
    area. This test does not reliably catch that: an adversarial check that shuffled one real
    month's nine values across areas passed here every time, because the cross-area spread
    within a single calm month is well under the threshold below. What it does catch is a
    grosser failure — an order-of-magnitude error, a sign flip, or a systematic area swap that
    persists across several months, which does show up as a spike against that area's own
    history.

    The observed maximum swing across the whole shipped table, including the 2022 energy-crisis
    months, is 2.68 円/kWh (Hokkaido, 2026-08 — independently confirmed against the rendered
    PDF). This threshold is set comfortably above that, not at it.

    Only a genuinely adjacent pair is compared. `scripts/refresh_adder_baseline.py` skips a
    month it cannot parse and leaves a documented gap (the same kind already in this table for
    2021-10 through 2022-05) rather than guessing — a pair spanning that gap is not a
    month-over-month comparison at all, and comparing it anyway would fail this test on a
    correct file for a reason unrelated to what it checks.
    """
    baseline = _load()

    for area in AREA_CODES:
        ordered = sorted(baseline.fuel_by_area[area], key=lambda r: r.valid_from)
        for earlier, later in itertools.pairwise(ordered):
            if earlier.valid_to != later.valid_from:
                continue
            delta = abs(later.price_inc_tax - earlier.price_inc_tax)
            assert delta <= Decimal("4.0"), (area, earlier, later, delta)


def test_a_known_month_prices_at_its_published_rate() -> None:
    """2026-01 Tokyo, read from the provider's own PDF: 2.44 円/kWh (税込)."""
    schedule = baseline_schedule("03")
    moment = datetime(2026, 1, 15, tzinfo=UTC)

    covering_fuel_records = [
        record
        for record in schedule.records
        if record.kind is AdderKind.FUEL_COST_ADJUSTMENT and record.applies_at(moment)
    ]

    assert len(covering_fuel_records) == 1
    assert covering_fuel_records[0].price_inc_tax == Decimal("2.44")
