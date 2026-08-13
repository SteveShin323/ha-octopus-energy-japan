"""When each time-of-use band applies.

A time-of-use tariff prices the same kilowatt-hour differently depending on the hour it was
consumed. The provider tells us the bands and their prices — `consumptionCharges` returns
`band: "CONSUMPTION_03_DAY"` with a price — but never the hours those bands cover.

The one field that would answer it, `Query.rateGroupTouScheme`, is refused with
`KT-CT-1111 Unauthorized` for every argument, on two separate accounts including one
actually on the EV tariff. A full introspection of the 2290-type schema on 2026-08-12 found
it to be the only field returning `TimeOfUseSchemeType`, and every path to
`VariantProfile.schemes` — the one other place the hours could hide — runs through
`availableProducts` or `agreementsForRollover`, both equally refused. There is no query that
returns the hours.

The hours are published instead, in the 電気料金メニュー定義書 for each tariff and each grid
area, at <https://octopusenergy.co.jp/terms>. This module carries them, transcribed from
those documents for all five schemes and all nine areas, and `docs/TOU_SCHEMES.md` records
which document each line came from.

Only the hours are carried here. Every price still comes from the customer's own agreement,
so nothing that the provider can change without telling us is hard-coded. A band the table
does not cover is refused rather than guessed, the same as any other tariff shape this
integration cannot express.

Two properties of the provider's naming make one table cover every area:

- a band reads `CONSUMPTION_{grid operator}_{slot}`, so it already says which area it is for;
- areas 06, 07 and 08 insert `HIGH_` or `LOW_` before the slot. That marks the contract
  capacity tier the price belongs to — `tariffSummary` reports the same split as
  `contractCapacityPattern: TIERED_HIGH`/`TIERED_LOW` — and the definition documents give one
  set of hours per area regardless. It is dropped when looking the hours up.

Japan Standard Time is used throughout, as a fixed +09:00 offset rather than a named zone:
Japan has observed no daylight saving since 1951, so the offset is exact, and building it
needs no time-zone database at import time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

JST: Final = timezone(timedelta(hours=9), "JST")

_BAND_PATTERN: Final = re.compile(r"^CONSUMPTION_(?P<area>\d{2})_(?:HIGH_|LOW_)?(?P<slot>.+)$")

_MINUTES_PER_HOUR: Final = 60
_MINUTES_PER_DAY: Final = 24 * _MINUTES_PER_HOUR


@dataclass(frozen=True, slots=True)
class DaySpan:
    """A half-open range of minutes from local midnight.

    `end` may be 1440, which is midnight at the end of the day — the definition documents
    write that as 午後12時. No span wraps past midnight: every band that would need to is
    instead the scheme's remainder, which is defined as whatever the others leave.
    """

    start: int
    end: int

    def contains(self, minute_of_day: int) -> bool:
        return self.start <= minute_of_day < self.end


@dataclass(frozen=True, slots=True)
class SeasonSpan:
    """An inclusive month-and-day range, which may wrap the turn of the year."""

    start_month: int
    start_day: int
    end_month: int
    end_day: int

    def contains(self, month: int, day: int) -> bool:
        start = (self.start_month, self.start_day)
        end = (self.end_month, self.end_day)
        here = (month, day)
        if start <= end:
            return start <= here <= end
        # December to February, say: inside if it is at or after the start, or at or before
        # the end.
        return here >= start or here <= end


@dataclass(frozen=True, slots=True)
class TouSlot:
    """One named part of a scheme, and when it applies.

    `spans` empty means the whole day, which is how a scheme that varies only by season is
    written. `seasons` empty means all year.
    """

    name: str
    spans: tuple[DaySpan, ...] = ()
    seasons: tuple[SeasonSpan, ...] = ()

    def covers(self, minute_of_day: int, month: int, day: int) -> bool:
        if self.seasons and not any(season.contains(month, day) for season in self.seasons):
            return False
        if not self.spans:
            return True
        return any(span.contains(minute_of_day) for span in self.spans)


@dataclass(frozen=True, slots=True)
class AreaSchedule:
    """The slots of one scheme in one grid area.

    `remainder` names the slot that covers every hour the explicit ones do not. Writing the
    overnight band that way is what the documents themselves do — 「デイタイム以外の時間帯」 —
    and it keeps a band that crosses midnight from needing arithmetic that wraps.
    """

    explicit: tuple[TouSlot, ...]
    remainder: str

    @property
    def slot_names(self) -> frozenset[str]:
        return frozenset({slot.name for slot in self.explicit} | {self.remainder})

    def slot_at(self, moment_jst: datetime) -> str | None:
        """Return the slot in force, or `None` if two explicit slots claim the same hour.

        An overlap means the transcription is wrong. Returning a slot anyway would price
        those hours by whichever was written first, so the caller refuses instead.
        """
        minute = moment_jst.hour * _MINUTES_PER_HOUR + moment_jst.minute
        matched = [
            slot.name
            for slot in self.explicit
            if slot.covers(minute, moment_jst.month, moment_jst.day)
        ]
        if len(matched) > 1:
            return None
        return matched[0] if matched else self.remainder


@dataclass(frozen=True, slots=True)
class TouScheme:
    """One provider time-of-use scheme, across every area it is sold in."""

    identifier: str
    by_area: Mapping[str, AreaSchedule]

    def area(self, grid_operator_code: str) -> AreaSchedule | None:
        return self.by_area.get(grid_operator_code)


# --- The transcribed schedules ------------------------------------------------------------
#
# Every span below is stated in the 電気料金メニュー定義書 for the tariff and area named in
# `docs/TOU_SCHEMES.md`. Hours are Japan Standard Time.

_AREAS: Final = ("01", "02", "03", "04", "05", "06", "07", "08", "09")

# 夏季 — 毎年7月1日から9月30日まで.
_SUMMER: Final = SeasonSpan(7, 1, 9, 30)
# 冬季 — 毎年12月1日から翌年の2月28日まで, or the 29th in a leap year. Written to the 29th so a
# leap day is inside it; the 28th of a common year is still the last day the range contains.
_WINTER: Final = SeasonSpan(12, 1, 2, 29)
# その他季 for an area whose only other season is summer.
_OUTSIDE_SUMMER: Final = SeasonSpan(10, 1, 6, 30)
# その他季 for an area that has a winter too: the two stretches summer and winter leave.
_OUTSIDE_SUMMER_AND_WINTER: Final = (SeasonSpan(3, 1, 6, 30), SeasonSpan(10, 1, 11, 30))


def _hours(*pairs: tuple[int, int]) -> tuple[DaySpan, ...]:
    """Build spans from whole-hour boundaries, the only kind the documents use.

    Every boundary in every one of the five schemes falls on a whole hour, which is why the
    cost projector can price an hour as a unit: no hour of consumption is ever split between
    two bands. A scheme transcribed with a boundary at a half hour would break that, and
    would have to be added to the projector as well as to this table.
    """
    return tuple(
        DaySpan(start * _MINUTES_PER_HOUR, end * _MINUTES_PER_HOUR) for start, end in pairs
    )


def _same_everywhere(schedule: AreaSchedule) -> dict[str, AreaSchedule]:
    return dict.fromkeys(_AREAS, schedule)


# EVオクトパス. Identical in all nine areas.
_EV: Final = TouScheme(
    identifier="tgoe_ev_tou_jan_25_scheme",
    by_area=_same_everywhere(
        AreaSchedule(
            explicit=(
                # EVデイタイム — 毎日午前11時から午後1時まで.
                TouSlot("DAY", _hours((11, 13))),
                # EVナイトタイム — 毎日午前1時から午前5時まで.
                TouSlot("NIGHT", _hours((1, 5))),
            ),
            # スタンダードタイム — EVナイトタイムおよびEVデイタイム以外の時間帯.
            remainder="STANDARD",
        )
    ),
)

# ソーラーオクトパス. Identical in all nine areas.
_SOLAR: Final = TouScheme(
    identifier="tgoe_solar_tou_scheme",
    by_area=_same_everywhere(
        AreaSchedule(
            explicit=(
                # ソーラータイム — 毎日午前8時から午後4時まで.
                TouSlot("SOLAR", _hours((8, 16))),
                # ホームタイム — 毎日午前6時から午前8時までおよび午後4時から午後10時まで.
                TouSlot("HOME", _hours((6, 8), (16, 22))),
            ),
            # ナイトタイム — ソーラータイムおよびホームタイム以外の時間帯.
            remainder="NIGHT",
        )
    ),
)

# オール電化オクトパス-サンシャイン. Identical in all nine areas.
_ALL_DENKA_SUNSHINE: Final = TouScheme(
    identifier="tgoe_all_denka_tou_mar_25_scheme",
    by_area=_same_everywhere(
        AreaSchedule(
            # デイタイム — 毎日午前9時から午後3時まで.
            explicit=(TouSlot("DAY", _hours((9, 15))),),
            # スタンダードタイム — デイタイム以外の時間帯.
            remainder="STANDARD",
        )
    ),
)

# 動力オクトパス and 共用部電力. Season only: the price does not change with the hour, so the
# slots cover the whole day and differ by season alone.
_POWER: Final = TouScheme(
    identifier="tgoe_power_tou_scheme",
    by_area=_same_everywhere(
        AreaSchedule(
            explicit=(TouSlot("SUMMER", seasons=(_SUMMER,)),),
            # その他季 — 毎年10月1日から翌年の6月30日まで, which is everything summer leaves.
            remainder="OTHER",
        )
    ),
)

# オール電化オクトパス. The only scheme whose hours differ by area.
_ALL_DENKA: Final = TouScheme(
    identifier="tgoe_all_denka_tou_scheme",
    by_area={
        # 毎日午前8時から午後10時まで.
        "01": AreaSchedule((TouSlot("DAY", _hours((8, 22))),), "NIGHT"),
        "02": AreaSchedule((TouSlot("DAY", _hours((8, 22))),), "NIGHT"),
        # 毎日午前0時から午前1時までおよび午前6時から午後12時まで. 午後12時 is midnight at the
        # end of the day, not noon, so the night band is the single stretch from 1 to 6.
        "03": AreaSchedule((TouSlot("DAY", _hours((0, 1), (6, 24))),), "NIGHT"),
        "04": AreaSchedule(
            (
                TouSlot("DAY", _hours((10, 17))),
                TouSlot("HOME", _hours((8, 10), (17, 22))),
            ),
            "NIGHT",
        ),
        "05": AreaSchedule((TouSlot("DAY", _hours((8, 20))),), "NIGHT"),
        # Kansai prices the same daytime hours differently by season, which is why the bands
        # read SUMMER_DAY and OTHER_DAY rather than DAY.
        "06": AreaSchedule(
            (
                TouSlot("SUMMER_DAY", _hours((10, 17)), (_SUMMER,)),
                TouSlot("OTHER_DAY", _hours((10, 17)), (_OUTSIDE_SUMMER,)),
                TouSlot("HOME", _hours((7, 10), (17, 23))),
            ),
            "NIGHT",
        ),
        "07": AreaSchedule(
            (
                TouSlot("SUMMER_DAY", _hours((9, 21)), (_SUMMER,)),
                TouSlot("OTHER_DAY", _hours((9, 21)), (_OUTSIDE_SUMMER,)),
            ),
            "NIGHT",
        ),
        "08": AreaSchedule((TouSlot("DAY", _hours((9, 23))),), "NIGHT"),
        # Kyushu groups summer and winter into one band and leaves spring and autumn as the
        # other, so its daytime split is three seasons wide but two bands deep.
        "09": AreaSchedule(
            (
                TouSlot("SUMMER_WINTER_DAY", _hours((8, 22)), (_SUMMER, _WINTER)),
                TouSlot("OTHER_DAY", _hours((8, 22)), _OUTSIDE_SUMMER_AND_WINTER),
            ),
            "NIGHT",
        ),
    },
)

SCHEMES: Final[Mapping[str, TouScheme]] = {
    scheme.identifier: scheme for scheme in (_EV, _SOLAR, _ALL_DENKA_SUNSHINE, _POWER, _ALL_DENKA)
}


def scheme_for(identifier: str | None) -> TouScheme | None:
    """Return the transcribed scheme with this provider identifier, if it is known."""
    if not identifier:
        return None
    return SCHEMES.get(identifier)


def split_band(band: str | None) -> tuple[str, str] | None:
    """Split a band into its grid operator code and slot name.

    Returns `None` for anything that is not shaped like a consumption band, which includes
    the stepped-tariff bands (`CONSUMPTION_STEPPED_03_01`): those name a step, not an hour,
    and reading one as a slot would invent a schedule for a tariff that has none.
    """
    if not band:
        return None
    matched = _BAND_PATTERN.match(band)
    if matched is None:
        return None
    slot = matched.group("slot")
    if slot.isdigit():
        return None
    return matched.group("area"), slot


def slot_at(scheme: TouScheme, grid_operator_code: str, moment: datetime) -> str | None:
    """Return the slot in force at a moment, or `None` if it cannot be decided.

    The moment may be in any time zone; it is converted to Japan Standard Time first. The
    season has to be read from the Japanese date rather than the UTC one — 15:00 UTC on the
    30th of June is already the 1st of July in Japan, and reading the UTC date would put nine
    hours of every seasonal boundary in the wrong season, twice a year.
    """
    area = scheme.area(grid_operator_code)
    if area is None:
        return None
    return area.slot_at(moment.astimezone(JST))
