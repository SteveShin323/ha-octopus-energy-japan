"""Tests for the transcribed time-of-use schedules.

The hours here are not an implementation detail: they are a copy of what the provider
publishes, and a wrong one produces a confident wrong bill rather than a visible failure.
So these tests check the transcription itself — the boundaries, the seasons, and the
completeness of the table — as much as the code that reads it.

The band names used below are the ones the provider actually returns. The three EV bands
come from the account that opened issue #93; the rest were read from `tariffSummary`, which
answers for every product in a grid area without an entitlement to it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from custom_components.octopus_energy_japan.api.tou import (
    JST,
    SCHEMES,
    AreaSchedule,
    DaySpan,
    TouSlot,
    scheme_for,
    slot_at,
    split_band,
)

EV = "tgoe_ev_tou_jan_25_scheme"
SOLAR = "tgoe_solar_tou_scheme"
SUNSHINE = "tgoe_all_denka_tou_mar_25_scheme"
POWER = "tgoe_power_tou_scheme"
ALL_DENKA = "tgoe_all_denka_tou_scheme"

AREAS = ("01", "02", "03", "04", "05", "06", "07", "08", "09")


def _jst(month: int, day: int, hour: int) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=JST)


def _slot(scheme_id: str, area: str, moment: datetime) -> str | None:
    scheme = scheme_for(scheme_id)
    assert scheme is not None
    return slot_at(scheme, area, moment)


# --- The EV schedule, which is what issue #93 asked for -----------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "STANDARD"),
        (1, "NIGHT"),
        (4, "NIGHT"),
        (5, "STANDARD"),
        (10, "STANDARD"),
        (11, "DAY"),
        (12, "DAY"),
        (13, "STANDARD"),
        (23, "STANDARD"),
    ],
)
def test_the_ev_schedule_matches_the_published_hours(hour: int, expected: str) -> None:
    """EVナイトタイム is 01:00-05:00 and EVデイタイム 11:00-13:00, both ends exclusive.

    The boundary hours are the ones worth pinning: an off-by-one here moves a whole hour of
    every day between the cheapest band and the most expensive one.
    """
    assert _slot(EV, "03", _jst(8, 13, hour)) == expected


def test_the_ev_schedule_is_the_same_in_every_area() -> None:
    """All nine definition documents state the same two windows."""
    for area in AREAS:
        assert _slot(EV, area, _jst(8, 13, 12)) == "DAY"
        assert _slot(EV, area, _jst(8, 13, 3)) == "NIGHT"
        assert _slot(EV, area, _jst(8, 13, 20)) == "STANDARD"


def test_a_utc_moment_is_read_in_japanese_time() -> None:
    """16:00 UTC is 01:00 the next day in Japan, which is the start of the night band."""
    assert _slot(EV, "03", datetime(2026, 8, 12, 16, tzinfo=UTC)) == "NIGHT"
    assert _slot(EV, "03", datetime(2026, 8, 12, 15, 59, tzinfo=UTC)) == "STANDARD"


# --- The schedules that vary by area or by season -----------------------------------------


def test_tokyo_all_denka_nights_are_the_gap_the_day_band_leaves() -> None:
    """デイタイム is 00:00-01:00 and 06:00-24:00, so the night is the single stretch between.

    午後12時 in that document is midnight at the end of the day, not noon. Read as noon it
    would price the whole afternoon and evening at the overnight rate.
    """
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 0)) == "DAY"
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 1)) == "NIGHT"
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 5)) == "NIGHT"
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 6)) == "DAY"
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 13)) == "DAY"
    assert _slot(ALL_DENKA, "03", _jst(8, 13, 23)) == "DAY"


def test_kansai_all_denka_splits_the_same_daytime_hours_by_season() -> None:
    """夏季 is 1 July to 30 September; everything else is その他季."""
    assert _slot(ALL_DENKA, "06", _jst(7, 1, 12)) == "SUMMER_DAY"
    assert _slot(ALL_DENKA, "06", _jst(9, 30, 12)) == "SUMMER_DAY"
    assert _slot(ALL_DENKA, "06", _jst(10, 1, 12)) == "OTHER_DAY"
    assert _slot(ALL_DENKA, "06", _jst(6, 30, 12)) == "OTHER_DAY"
    # The season never reaches the hours outside the daytime window.
    assert _slot(ALL_DENKA, "06", _jst(7, 15, 8)) == "HOME"
    assert _slot(ALL_DENKA, "06", _jst(7, 15, 3)) == "NIGHT"


def test_kyushu_all_denka_groups_summer_and_winter_into_one_band() -> None:
    """夏季 and 冬季 share a band; spring and autumn are the other."""
    assert _slot(ALL_DENKA, "09", _jst(8, 1, 12)) == "SUMMER_WINTER_DAY"
    assert _slot(ALL_DENKA, "09", _jst(1, 15, 12)) == "SUMMER_WINTER_DAY"
    assert _slot(ALL_DENKA, "09", _jst(12, 1, 12)) == "SUMMER_WINTER_DAY"
    assert _slot(ALL_DENKA, "09", _jst(4, 1, 12)) == "OTHER_DAY"
    assert _slot(ALL_DENKA, "09", _jst(11, 1, 12)) == "OTHER_DAY"


def test_the_season_is_read_from_the_japanese_date() -> None:
    """15:00 UTC on 30 June is already 1 July in Japan, and so already summer.

    Reading the UTC date instead would put nine hours of each seasonal boundary in the wrong
    season, twice a year. The season-only scheme shows it because it is the one whose bands
    reach the hours around Japanese midnight, which is where the two dates disagree.
    """
    assert _slot(POWER, "03", datetime(2026, 6, 30, 14, tzinfo=UTC)) == "OTHER"
    assert _slot(POWER, "03", datetime(2026, 6, 30, 15, tzinfo=UTC)) == "SUMMER"
    assert _slot(POWER, "03", datetime(2026, 9, 30, 14, tzinfo=UTC)) == "SUMMER"
    assert _slot(POWER, "03", datetime(2026, 9, 30, 15, tzinfo=UTC)) == "OTHER"


def test_the_power_schedule_changes_with_the_season_only() -> None:
    """動力オクトパス prices by season; the hour never enters into it."""
    for hour in (0, 6, 12, 23):
        assert _slot(POWER, "03", _jst(8, 1, hour)) == "SUMMER"
        assert _slot(POWER, "03", _jst(2, 1, hour)) == "OTHER"


def test_the_solar_schedule_covers_the_two_home_stretches() -> None:
    """ホームタイム is two windows, and the night is what the others leave."""
    assert _slot(SOLAR, "03", _jst(8, 13, 6)) == "HOME"
    assert _slot(SOLAR, "03", _jst(8, 13, 7)) == "HOME"
    assert _slot(SOLAR, "03", _jst(8, 13, 8)) == "SOLAR"
    assert _slot(SOLAR, "03", _jst(8, 13, 15)) == "SOLAR"
    assert _slot(SOLAR, "03", _jst(8, 13, 16)) == "HOME"
    assert _slot(SOLAR, "03", _jst(8, 13, 21)) == "HOME"
    assert _slot(SOLAR, "03", _jst(8, 13, 22)) == "NIGHT"
    assert _slot(SOLAR, "03", _jst(8, 13, 5)) == "NIGHT"


def test_the_sunshine_schedule_is_a_single_daytime_window() -> None:
    assert _slot(SUNSHINE, "03", _jst(8, 13, 9)) == "DAY"
    assert _slot(SUNSHINE, "03", _jst(8, 13, 14)) == "DAY"
    assert _slot(SUNSHINE, "03", _jst(8, 13, 15)) == "STANDARD"


# --- Properties the whole table has to hold -----------------------------------------------


def test_every_scheme_covers_every_grid_area() -> None:
    """A customer in an area the table skipped would silently lose their cost statistic."""
    for identifier, scheme in SCHEMES.items():
        assert set(scheme.by_area) == set(AREAS), identifier


@pytest.mark.parametrize("scheme_id", list(SCHEMES))
def test_every_hour_of_the_year_resolves_to_exactly_one_slot(scheme_id: str) -> None:
    """No hour may be claimed by two bands, and none may be left with no band at all.

    Two explicit slots overlapping is a transcription error, and the schedule reports it by
    refusing rather than by picking the first. This walks every hour of a leap year in every
    area so a season boundary written the wrong way round cannot hide.
    """
    scheme = scheme_for(scheme_id)
    assert scheme is not None
    for area in AREAS:
        schedule = scheme.area(area)
        assert schedule is not None
        for month, days in ((1, 31), (2, 29), (6, 30), (7, 31), (9, 30), (10, 31), (12, 31)):
            for day in (1, days // 2, days):
                for hour in range(24):
                    moment = datetime(2024, month, day, hour, tzinfo=JST)
                    slot = schedule.slot_at(moment)
                    assert slot is not None, (scheme_id, area, moment)
                    assert slot in schedule.slot_names


# --- Reading a band name ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("band", "expected"),
    [
        ("CONSUMPTION_03_DAY", ("03", "DAY")),
        ("CONSUMPTION_03_NIGHT", ("03", "NIGHT")),
        ("CONSUMPTION_03_STANDARD", ("03", "STANDARD")),
        ("CONSUMPTION_09_SUMMER_WINTER_DAY", ("09", "SUMMER_WINTER_DAY")),
        # Areas 06 to 08 mark which contract capacity tier the price belongs to. The hours are
        # the same either way, so the marker is dropped.
        ("CONSUMPTION_06_HIGH_DAY", ("06", "DAY")),
        ("CONSUMPTION_08_LOW_STANDARD", ("08", "STANDARD")),
    ],
)
def test_a_band_names_its_area_and_its_slot(band: str, expected: tuple[str, str]) -> None:
    assert split_band(band) == expected


@pytest.mark.parametrize(
    "band",
    [
        None,
        "",
        # A stepped tariff's bands number the steps. Reading one as a slot would invent a
        # schedule for a tariff that has none.
        "CONSUMPTION_STEPPED_03_01",
        "CONSUMPTION_03_01",
        "SOMETHING_ELSE",
    ],
)
def test_a_band_that_does_not_name_an_hour_is_not_split(band: str | None) -> None:
    assert split_band(band) is None


def test_an_unknown_scheme_has_no_schedule() -> None:
    assert scheme_for("tgoe_something_new_scheme") is None
    assert scheme_for(None) is None


def test_a_scheme_is_not_sold_outside_the_areas_it_lists() -> None:
    scheme = scheme_for(EV)
    assert scheme is not None
    assert slot_at(scheme, "10", _jst(8, 13, 12)) is None


def test_two_slots_claiming_the_same_hour_are_reported_rather_than_ranked() -> None:
    """An overlap is a transcription error, and picking the first would hide it.

    The table has none — `test_every_hour_of_the_year_resolves_to_exactly_one_slot` is what
    keeps it that way — so this builds one to check the schedule says so instead of guessing.
    """
    overlapping = AreaSchedule(
        explicit=(
            TouSlot("DAY", (DaySpan(9 * 60, 17 * 60),)),
            TouSlot("HOME", (DaySpan(16 * 60, 22 * 60),)),
        ),
        remainder="NIGHT",
    )

    assert overlapping.slot_at(_jst(8, 13, 16)) is None
    assert overlapping.slot_at(_jst(8, 13, 10)) == "DAY"
    assert overlapping.slot_at(_jst(8, 13, 20)) == "HOME"
    assert overlapping.slot_at(_jst(8, 13, 2)) == "NIGHT"
