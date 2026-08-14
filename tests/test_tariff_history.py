"""Tests for the archive of rate adjustments the API stops serving."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from custom_components.octopus_energy_japan.api.tariff import (
    SupplyPointTariff,
    TariffAdder,
    TariffStep,
)
from custom_components.octopus_energy_japan.tariff_history import (
    TARIFF_HISTORY_SCHEMA_VERSION,
    AdderKind,
    AdderSchedule,
    ArchivedAdder,
    TariffHistoryError,
    baseline_covered_hours,
    deserialize_adders,
    live_schedule,
    merge_observed,
    migrate_tariff_history_payload,
    observed_adders,
    serialize_adders,
    with_baseline,
)

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
# The JST calendar months a real account's adjustments were observed to run over.
JULY = (datetime(2026, 6, 30, 15, tzinfo=UTC), datetime(2026, 7, 31, 15, tzinfo=UTC))
AUGUST = (datetime(2026, 7, 31, 15, tzinfo=UTC), datetime(2026, 8, 31, 15, tzinfo=UTC))


def _record(
    window: tuple[datetime, datetime | None] = AUGUST,
    price: str = "4.32",
    *,
    kind: AdderKind = AdderKind.FUEL_COST_ADJUSTMENT,
    revisions: int = 0,
) -> ArchivedAdder:
    return ArchivedAdder(
        kind=kind,
        valid_from=window[0],
        valid_to=window[1],
        price_inc_tax=Decimal(price),
        first_observed_at=NOW,
        revisions=revisions,
    )


def _tariff(
    fuel: TariffAdder | None = None,
    levy: TariffAdder | None = None,
) -> SupplyPointTariff:
    return SupplyPointTariff(
        account_number="A-1",
        supply_point_id="SP-1",
        product_code="P",
        product_name="P",
        steps=(TariffStep(Decimal(0), None, Decimal("20.62")),),
        standing_charge_per_day=None,
        fuel_cost_adjustment=fuel,
        renewable_energy_levy=levy,
    )


def test_a_record_must_carry_a_period_it_can_be_filed_under() -> None:
    with pytest.raises(ValueError, match="cover a period"):
        ArchivedAdder(
            kind=AdderKind.FUEL_COST_ADJUSTMENT,
            valid_from=AUGUST[1],
            valid_to=AUGUST[0],
            price_inc_tax=Decimal("4.32"),
            first_observed_at=NOW,
        )


def test_a_record_needs_timezone_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ArchivedAdder(
            kind=AdderKind.FUEL_COST_ADJUSTMENT,
            valid_from=datetime(2026, 8, 1),  # noqa: DTZ001 - the point of the test
            valid_to=None,
            price_inc_tax=Decimal("4.32"),
            first_observed_at=NOW,
        )


def test_an_hour_inside_a_stored_period_uses_that_period_s_price() -> None:
    schedule = AdderSchedule((_record(JULY, "3.00"), _record(AUGUST, "4.32")))

    rate = schedule.rate_at(datetime(2026, 7, 15, tzinfo=UTC))

    assert rate.total == Decimal("3.00")
    assert rate.extrapolated is False


def test_an_hour_before_the_archive_begins_uses_its_earliest_price() -> None:
    """Reaching to the near end is the smallest extrapolation available.

    Using the newest stored value instead would price a two-year-old hour with this month's
    adjustment, and a Japanese fuel-cost adjustment moves by several yen per kWh and changes
    sign — a confident wrong number of unbounded size.
    """
    schedule = AdderSchedule((_record(JULY, "3.00"), _record(AUGUST, "4.32")))

    rate = schedule.rate_at(datetime(2025, 1, 1, tzinfo=UTC))

    assert rate.total == Decimal("3.00")
    assert rate.extrapolated is True


def test_an_hour_after_the_archive_ends_uses_its_latest_price() -> None:
    schedule = AdderSchedule((_record(JULY, "3.00"), _record(AUGUST, "4.32")))

    rate = schedule.rate_at(datetime(2027, 1, 1, tzinfo=UTC))

    assert rate.total == Decimal("4.32")
    assert rate.extrapolated is True


def test_the_kinds_are_summed_independently() -> None:
    """A missing levy must not drag the fuel adjustment out of its own period."""
    schedule = AdderSchedule(
        (
            _record(AUGUST, "4.32"),
            _record(JULY, "4.18", kind=AdderKind.RENEWABLE_ENERGY_LEVY),
        )
    )

    rate = schedule.rate_at(datetime(2026, 8, 4, tzinfo=UTC))

    # August's fuel adjustment exactly, plus July's levy clamped forward.
    assert rate.total == Decimal("8.50")
    assert rate.extrapolated is True


def test_an_empty_archive_contributes_nothing() -> None:
    rate = AdderSchedule().rate_at(NOW)

    assert rate.total == Decimal(0)
    assert rate.extrapolated is False


def test_an_overlapping_restatement_resolves_to_the_later_start() -> None:
    schedule = AdderSchedule(
        (
            _record((AUGUST[0], None), "4.32"),
            _record((datetime(2026, 8, 10, tzinfo=UTC), None), "5.00"),
        )
    )

    assert schedule.rate_at(datetime(2026, 8, 20, tzinfo=UTC)).total == Decimal("5.00")


def test_a_new_period_is_filed_and_an_unchanged_one_is_not_rewritten() -> None:
    """The provider restates the same window until it expires.

    Saving on every refresh would rewrite the file twice a day forever for no new information.
    """
    first = merge_observed(
        (),
        [(AdderKind.FUEL_COST_ADJUSTMENT, TariffAdder(Decimal("4.32"), valid_from=AUGUST[0]))],
        observed_at=NOW,
    )
    assert first is not None
    assert len(first) == 1

    again = merge_observed(
        first,
        [(AdderKind.FUEL_COST_ADJUSTMENT, TariffAdder(Decimal("4.32"), valid_from=AUGUST[0]))],
        observed_at=NOW,
    )
    assert again is None


def test_a_restated_price_overwrites_and_counts_the_revision() -> None:
    """The provider's current statement about its own period is the authority.

    It is also what keeps the live tariff and the newest archived record from disagreeing.
    """
    existing = (_record((AUGUST[0], None), "4.32"),)

    merged = merge_observed(
        existing,
        [(AdderKind.FUEL_COST_ADJUSTMENT, TariffAdder(Decimal("5.00"), valid_from=AUGUST[0]))],
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert merged is not None
    assert merged[0].price_inc_tax == Decimal("5.00")
    assert merged[0].revisions == 1
    # When it was first learned is preserved; only the price moved.
    assert merged[0].first_observed_at == NOW


def test_an_adjustment_with_no_period_is_not_filed() -> None:
    """A record with no start satisfies every moment, so it would price the whole archive."""
    merged = merge_observed(
        (),
        [(AdderKind.FUEL_COST_ADJUSTMENT, TariffAdder(Decimal("4.32")))],
        observed_at=NOW,
    )

    assert merged is None


def test_an_observed_window_always_wins_over_the_baseline() -> None:
    observed = (_record(AUGUST, "5.00"),)
    baseline = (_record(AUGUST, "1.23", kind=AdderKind.FUEL_COST_ADJUSTMENT),)

    merged = with_baseline(observed, baseline)

    assert merged == (observed[0],)


def test_the_baseline_fills_a_window_the_archive_has_never_observed() -> None:
    observed = (_record(AUGUST, "5.00"),)
    baseline = (_record(JULY, "1.23"),)

    merged = with_baseline(observed, baseline)

    assert set(merged) == {observed[0], baseline[0]}


def test_a_baseline_covered_hour_is_not_extrapolated() -> None:
    baseline = (_record(JULY, "1.23"),)

    merged = with_baseline((), baseline)
    rate = AdderSchedule(merged).rate_at(JULY[0])

    assert rate.total == Decimal("1.23")
    assert rate.extrapolated is False


def test_baseline_covered_hours_counts_only_the_baseline_s_own_stamp() -> None:
    generated_at = datetime(2026, 8, 1, tzinfo=UTC)
    baseline_record = ArchivedAdder(
        kind=AdderKind.FUEL_COST_ADJUSTMENT,
        valid_from=JULY[0],
        valid_to=JULY[1],
        price_inc_tax=Decimal("1.23"),
        first_observed_at=generated_at,
    )
    observed_record = _record(AUGUST, "5.00")
    schedule = AdderSchedule((baseline_record, observed_record))

    count = baseline_covered_hours(
        [JULY[0], datetime(2026, 7, 15, tzinfo=UTC), AUGUST[0]],
        schedule,
        generated_at,
    )

    assert count == 2


def test_the_live_schedule_holds_only_what_the_provider_states_now() -> None:
    tariff = _tariff(
        fuel=TariffAdder(Decimal("4.32"), valid_from=AUGUST[0], valid_to=AUGUST[1]),
        levy=TariffAdder(Decimal("4.18")),
    )

    schedule = live_schedule(tariff, observed_at=NOW)

    # The levy has no period, so it cannot be placed on a timeline at all.
    assert [record.kind for record in schedule.records] == [AdderKind.FUEL_COST_ADJUSTMENT]
    assert len(observed_adders(tariff)) == 2


def test_an_archive_round_trips_without_losing_precision() -> None:
    records = (
        _record(JULY, "3.005", kind=AdderKind.RENEWABLE_ENERGY_LEVY),
        _record(AUGUST, "4.32", revisions=2),
    )

    assert deserialize_adders(serialize_adders(records)) == tuple(sorted(records))


def test_a_payload_is_written_in_a_stable_order() -> None:
    """A byte-stable file makes a diff meaningful and a save idempotent."""
    records = (_record(AUGUST, "4.32"), _record(JULY, "3.00"))

    payload = serialize_adders(records)

    assert [item["valid_from"] for item in payload["adders"]] == [
        "2026-06-30T15:00:00Z",
        "2026-07-31T15:00:00Z",
    ]


def test_a_newer_schema_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(TariffHistoryError, match="newer"):
        migrate_tariff_history_payload({"schema_version": TARIFF_HISTORY_SCHEMA_VERSION + 1})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": "1"},
        {"schema_version": True},
        {"schema_version": 0},
    ],
)
def test_a_payload_with_no_usable_version_is_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(TariffHistoryError):
        migrate_tariff_history_payload(payload)


@pytest.mark.parametrize(
    "adders",
    [
        "not-a-list",
        ["not-a-mapping"],
        [{"kind": "unknown", "valid_from": "2026-08-01T00:00:00Z", "price_inc_tax": "1"}],
        [{"kind": "fuel_cost_adjustment", "price_inc_tax": "1"}],
        [{"kind": "fuel_cost_adjustment", "valid_from": "nope", "price_inc_tax": "1"}],
        [
            {
                "kind": "fuel_cost_adjustment",
                "valid_from": "2026-08-01T00:00:00Z",
                "price_inc_tax": "1",
                "first_observed_at": "2026-08-01T00:00:00Z",
                "revisions": -1,
            }
        ],
    ],
)
def test_an_unreadable_archive_raises_rather_than_returning_part_of_itself(
    adders: object,
) -> None:
    """This is the only copy of values the provider will not serve again.

    Reading the records that happen to parse and dropping the rest would lose data with no way
    to notice, so the whole payload is refused and the caller quarantines it.
    """
    with pytest.raises(TariffHistoryError):
        deserialize_adders({"schema_version": TARIFF_HISTORY_SCHEMA_VERSION, "adders": adders})


def _payload(**overrides: Any) -> dict[str, Any]:
    record = {
        "kind": "fuel_cost_adjustment",
        "valid_from": "2026-07-31T15:00:00Z",
        "valid_to": "2026-08-31T15:00:00Z",
        "price_inc_tax": "4.32",
        "first_observed_at": "2026-08-04T12:00:00Z",
    }
    record.update(overrides)
    return {"schema_version": TARIFF_HISTORY_SCHEMA_VERSION, "adders": [record]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"valid_to": 12345},
        {"valid_from": 12345},
        {"price_inc_tax": 4.32},
        {"price_ex_tax": 3.93},
        {"price_inc_tax": "NaN"},
        {"first_observed_at": "not a timestamp"},
        {"revisions": "two"},
    ],
)
def test_a_record_this_cannot_read_exactly_is_refused(overrides: dict[str, Any]) -> None:
    """Every field is refused rather than coerced, for the same reason as the whole payload."""
    with pytest.raises(TariffHistoryError):
        deserialize_adders(_payload(**overrides))


def test_a_record_written_before_revisions_existed_reads_as_none() -> None:
    payload = _payload()
    payload["adders"][0].pop("revisions", None)

    (record,) = deserialize_adders(payload)

    assert record.revisions == 0
    assert record.price_ex_tax is None


def test_an_open_ended_record_round_trips_with_no_end() -> None:
    (record,) = deserialize_adders(_payload(valid_to=None))

    assert record.valid_to is None
