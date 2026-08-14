# The shipped adder baseline

`custom_components/octopus_energy_japan/data/adder_baseline.json`, read by
`adder_baseline.py`, carries the two per-kWh adders — 燃料費調整額 (fuel cost adjustment) and
再エネ賦課金 (renewable energy levy) — from before any one account was ever connected. This
file records where those numbers came from, why they are shipped at all, and how to refresh
them.

## Why this exists

`tariff_history.py` archives what one installation's own account has actually reported, and
only from whenever that account was connected — the API serves only the period currently in
force, so an hour from a replaced period can never be re-fetched. Before the archive has
anything, `AdderSchedule.rate_at` falls back to the nearest value it does hold, which for an
hour more than a month or two in the past can be off by several yen.

Both adders are public record, independent of any one customer's account, so this does not
have to be a guess:

- **再エネ賦課金** is a single nationwide rate the Ministry of Economy, Trade and Industry sets
  once a fiscal year (May through the following April), identical for every retailer and every
  region. METI's own announcements confirm the same figures the provider publishes.
- **燃料費調整額** is published every month by the provider itself, at
  <https://octopusenergy.co.jp/terms>, under "燃料費調整額のお知らせ" — one PDF per month,
  since at least October 2021.

Shipping one verified copy means every installation prices its pre-connection history the same
way, correctly, instead of each one falling back to its own nearest-value guess — and instead
of every installation fetching the same public PDFs itself.

## Where the fuel cost adjustment PDF comes from, and its shape

Each monthly PDF (e.g. `2026-01.pdf`) is titled "YYYY年M月分の燃料費調整のお知らせ" and covers
**all nine grid areas in one file**, as a fixed-order table:

| Area code | Area | Area code | Area | Area code | Area |
|---|---|---|---|---|---|
| 01 | Hokkaido | 04 | Chubu | 07 | Chugoku |
| 02 | Tohoku | 05 | Hokuriku | 08 | Shikoku |
| 03 | Tokyo | 06 | Kansai | 09 | Kyushu |

(Same codes and area names as `docs/TOU_SCHEMES.md`.) The value is tax-included (税込), the
only figure the notice publishes. The Kyushu figure includes a bundled remote-island
universal-service adjustment (離島ユニバーサルサービス調整単価) — the notice says so, and it
is used as-is rather than split out, because the provider bills it as one figure.

The renewable energy levy PDF (`fy2026_.pdf` etc.) is one nationwide figure per fiscal year,
also tax-included, covering the JST calendar window "YYYY年5月分からYYYY+1年4月分まで."

### Extraction

Both are read from `pdfplumber.extract_tables()`. The fuel cost adjustment table's nine values
land in the fixed area order above regardless of which of two observed PDF layouts a given
month uses (an older chart-annotated one with the numbers as bare cells, and a newer plain
table with `"北海道電力エリア", "3.88 円/kWh"`-style rows) — the numeric part is pulled out
with a regex rather than assumed to be the whole cell.

### Known gaps in the current table

- **2021-10 through 2021-12** published one nationwide figure with no area breakdown at all —
  the per-area table format started with the 2022-01 notice. These three months are not in the
  baseline; an hour in them still falls back to the nearest baseline month via the existing
  extrapolation in `AdderSchedule.rate_at`.
- **2022-01 through 2022-05** have no notice on the provider's site at all (not merely
  unparsable) — likely predating some later area's launch. Same fallback applies.
- Four PDFs (`2022-06`, `2023-07`, `2024-12`, `2025-10`) have broken font encoding that
  `pdfplumber` cannot read as text — the same class of issue `docs/TOU_SCHEMES.md` already
  documents for two TOU documents. These were read visually (`pdftoppm -r 150 -png`) and
  transcribed by hand into the baseline.

## The fuel cost adjustment exemption

As of this writing, some products — シンプルオクトパス and しかたこシンプル — are billed with
**no fuel cost adjustment at all**, stated on the same monthly notice ("＊シンプルオクトパス、
しかたこシンプルには燃料費調整単価はありません"). The baseline has no way to tell a supply
point is on such a product before its account has been read at least once, so
`adders_for` in `__init__.py` offers the fuel-cost-adjustment baseline until the live tariff
has confirmed the product carries none (`tariff.fuel_cost_adjustment is None` on a tariff that
is not itself `None`) — see `baseline_schedule`'s `include_fuel_cost_adjustment` parameter.
Never assumed from a product code, because none has been observed and recorded for these
products yet.

## How the baseline is folded in

An observed record — this account's own archive — always wins over the baseline for the same
window; the baseline only ever covers a window the archive has nothing for
(`tariff_history.with_baseline`). Nothing is written to the per-account archive file; the
baseline is folded in fresh on every read, in `adders_for` (`__init__.py`).

A hint that a priced hour came from the baseline rather than the account's own archive: every
baseline record shares the exact same `first_observed_at`, stamped once at
`data/adder_baseline.json`'s `fetched_at` — `tariff_history.baseline_covered_hours` uses this
to count such hours for `report["baseline_adder_hours"]` in diagnostics, the same way
`extrapolated_adder_hours` is counted.

## Refreshing the table

`scripts/refresh_adder_baseline.py` automates this. It reads
<https://octopusenergy.co.jp/terms>, finds every fuel-cost-adjustment and renewable-energy-levy
PDF link, and for each one whose `(valid_from, valid_to)` window is not already in
`data/adder_baseline.json`, downloads and parses it (`pdfplumber`, handling both observed PDF
layouts) and appends the result. Two things it deliberately never does:

- **It never re-fetches or touches a window already on disk.** A month already in the file —
  including the four transcribed by hand because their PDF's font encoding defeats
  `pdfplumber` — is left exactly as it is. Only a genuinely new window triggers a download, so
  the script cannot silently drop a hand-transcribed month just because it can no longer
  re-parse the PDF behind it.
- **It refuses to write a file with fewer records than what is already committed**, in any area
  or in the levy table, as a backstop against a future bug in the merge logic.

`fetched_at` — a single top-level timestamp shared by every record (there is no per-record
stamp) — is only bumped when a record was actually added, which is what makes a re-run against
an unchanged source produce a byte-identical file: run it locally with `--dry-run` to see what
would change without writing anything.

A month whose PDF cannot be parsed (broken font encoding, or a layout this script does not yet
recognise) is reported on stdout and left out, exactly like the four current exceptions — read
it visually with `pdftoppm -r 150 -png` and add the record by hand, the same way those four
were done.

PDF-parsing dependencies (`pdfplumber`) live in `pyproject.toml`'s `baseline-refresh` optional
dependency group, not in `manifest.json` — the deployed integration only ever reads the static
JSON this script produces, never PDFs itself.

`.github/workflows/refresh_adder_baseline.yml` runs this monthly and opens a pull request when
something changed — it never merges automatically, and it runs `tests/test_adder_baseline.py`
against the regenerated file before opening the PR, so a parsing regression fails the job
instead of reaching review as a plausible-looking diff.
