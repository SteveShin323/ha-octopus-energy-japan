# Time-of-use schedules

`custom_components/octopus_energy_japan/api/tou.py` carries the hours each time-of-use band
covers. This file records where each of those hours came from and why they are carried at all.

Nothing else in this integration hard-codes a tariff. This does, and the reason is narrow: the
provider publishes the hours only as prose in its tariff documents, and refuses every API field
that would return them.

## Why the hours cannot be read from the API

`consumptionCharges` gives the band and its price:

```json
{ "band": "CONSUMPTION_03_DAY", "timeOfUse": "EV_DAY_TIME", "pricePerUnitIncTax": "12.6" }
```

It never gives the hours. Measured on 2026-08-12 and 2026-08-13:

| Field | Result |
|---|---|
| `Query.rateGroupTouScheme(specificationIdentifier, rateGroupCode)` | `KT-CT-1111 Unauthorized` for every argument tried, including the real scheme identifier |
| `Query.availableProducts(marketName)` → `rates.variantProfile.schemes` | `KT-CT-1111` |
| `Query.agreementsForRollover(daysBeforeExpiration, windowSize)` → same path | `KT-CT-1111` |
| `Query.agreementRates(agreementId)` | `KT-CT-1111` |
| `TimeOfUseOverrideType` | No field in the schema returns it |
| `Agreement.params` | `{}` on the account measured |

A full introspection of the 2290-type schema found `rateGroupTouScheme` to be the **only** field
returning `TimeOfUseSchemeType`, and `TimeSlotWithActivationRuleType` to be reachable only
through it. The refusal is not an argument error: the documented argument failures
(`KT-CT-12010`, `KT-CT-12049`) and the disabled-field error (`KT-CT-1113`) never appeared, so the
request is rejected before the arguments are resolved. It was refused identically on a second
account that is actually on the EV tariff, reported in
[issue #93](https://github.com/SteveShin323/ha-octopus-energy-japan/issues/93).

## What the API does give

`Query.tariffSummary(gridOperatorCode!, productCode)` is open — no entitlement to the product is
needed, and omitting `productCode` returns the whole catalogue for that area. Its
`productParams` names the schedule:

```json
{ "product_type": "time_of_use_product", "time_of_use_scheme": "tgoe_ev_tou_jan_25_scheme" }
```

The same blob appears on the agreement's own product as `params`, which is where the integration
reads it first; `tariffSummary` is the fallback for an agreement whose `params` arrives empty.

Two more measured facts shape the code:

- **Time of use and steps never combine.** Every time-of-use product in every grid area returns
  its consumption rates with `stepStart` and `stepEnd` null. A tariff is priced by the hour or by
  cumulative kWh, never both.
- **There are five schemes.** Across all ten grid operator codes the catalogue names no others.

## Band naming

```
CONSUMPTION_{grid operator code}_[HIGH_|LOW_]{slot}
```

The grid operator code makes a band self-describing, so one table serves every area.
`HIGH_`/`LOW_` appears in areas 06, 07 and 08 and marks the contract capacity tier the price
belongs to — `tariffSummary` reports the same split as `contractCapacityPattern: TIERED_HIGH` /
`TIERED_LOW`. The definition documents give one set of hours per area regardless of tier, so the
marker is dropped when looking the hours up.

## The schedules

Source: 電気料金メニュー定義書, <https://octopusenergy.co.jp/terms>, read 2026-08-13. One document
per tariff per grid area; the version in the file name matches `productParams.version`, which is
how a product is joined to its document.

Area codes: 01 Hokkaido, 02 Tohoku, 03 Tokyo, 04 Chubu, 05 Hokuriku, 06 Kansai, 07 Chugoku,
08 Shikoku, 09 Kyushu. All hours are Japan Standard Time. Every boundary falls on a whole hour,
which is why an hour of consumption never has to be split between two bands.

### `tgoe_ev_tou_jan_25_scheme` — EVオクトパス

Identical in all nine areas.

| Slot | Hours | Document wording |
|---|---|---|
| `NIGHT` | 01:00–05:00 | 毎日午前1時から午前5時までの時間帯 |
| `DAY` | 11:00–13:00 | 毎日午前11時から午後1時までの時間帯 |
| `STANDARD` | everything else | EVナイトタイムおよびEVデイタイム以外の時間帯 |

### `tgoe_solar_tou_scheme` — ソーラーオクトパス

Identical in all nine areas.

| Slot | Hours |
|---|---|
| `SOLAR` | 08:00–16:00 |
| `HOME` | 06:00–08:00 and 16:00–22:00 |
| `NIGHT` | everything else |

### `tgoe_all_denka_tou_mar_25_scheme` — オール電化オクトパス-サンシャイン

Identical in all nine areas.

| Slot | Hours |
|---|---|
| `DAY` | 09:00–15:00 |
| `STANDARD` | everything else |

### `tgoe_power_tou_scheme` — 動力オクトパス, 共用部電力

The price changes with the season, not the hour.

| Slot | Period |
|---|---|
| `SUMMER` | 1 July to 30 September |
| `OTHER` | everything else |

### `tgoe_all_denka_tou_scheme` — オール電化オクトパス

The only scheme whose hours differ by area.

| Area | Daytime slot | `HOME` | `NIGHT` |
|---|---|---|---|
| 01 | `DAY` 08:00–22:00 | — | rest |
| 02 | `DAY` 08:00–22:00 | — | rest |
| 03 | `DAY` 00:00–01:00 and 06:00–24:00 | — | rest (01:00–06:00) |
| 04 | `DAY` 10:00–17:00 | 08:00–10:00 and 17:00–22:00 | rest |
| 05 | `DAY` 08:00–20:00 | — | rest |
| 06 | `SUMMER_DAY` / `OTHER_DAY` 10:00–17:00 | 07:00–10:00 and 17:00–23:00 | rest |
| 07 | `SUMMER_DAY` / `OTHER_DAY` 09:00–21:00 | — | rest |
| 08 | `DAY` 09:00–23:00 | — | rest |
| 09 | `SUMMER_WINTER_DAY` / `OTHER_DAY` 08:00–22:00 | — | rest |

Seasons, where the daytime price is split:

- 夏季 — 1 July to 30 September (areas 06, 07, 09)
- 冬季 — 1 December to 28 February, or the 29th in a leap year (area 09 only)
- その他季 — what is left

Tokyo's document reads 毎日午前0時から午前1時までおよび午前6時から**午後12時**まで. 午後12時 there is
midnight at the end of the day, not noon. Read as noon it would price the whole afternoon and
evening at the overnight rate.

## Reading the documents again

Most are extractable with `pdftotext -layout`. The サンシャイン and ソーラー documents are not: their
embedded fonts carry no Unicode mapping and the extraction yields punctuation only. Render those
with `pdftoppm -r 130 -png -f 3 -l 4` and read the pages.

Times are written as `(午前|午後)N時[M分]から(午前|午後)N時[M分]まで`. Converting: 午前12時 is 00:00,
午後12時 is 24:00, any other 午後 hour is plus twelve.

## When a scheme is not in this table

The tariff is refused with `time_of_use_scheme_unknown` and no cost statistic is published.
Consumption and energy statistics are unaffected. A tariff whose agreement priced only some of
the slots its scheme defines is refused with `time_of_use_bands_incomplete`, because charging the
unpriced hours at nothing would understate every period they fall in.

Adding a scheme means transcribing its documents for all nine areas, adding it to
`SCHEMES` in `api/tou.py`, and extending this file. The test
`test_every_hour_of_the_year_resolves_to_exactly_one_slot` walks every area of every scheme and
fails if a new one leaves an hour uncovered or claimed twice.
