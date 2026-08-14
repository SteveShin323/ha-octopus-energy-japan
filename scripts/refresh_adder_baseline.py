#!/usr/bin/env python3
"""Refresh `data/adder_baseline.json` from OEJP's own public disclosure page.

See `docs/ADDER_BASELINE.md` for what this file is, why it exists, and the manual process this
script automates. Two things matter more here than in a typical refresh script:

- **Never delete a window.** A handful of the already-committed months had to be transcribed by
  hand from a rendered image because their PDF's font encoding defeats `pdfplumber` (documented
  in `docs/ADDER_BASELINE.md`'s "Known gaps" section). This script only ever adds a window it
  does not already have on disk — it never regenerates the file from scratch — so it can never
  silently drop one of those months just because it cannot re-parse the PDF behind it a second
  time. A month whose PDF still won't parse (broken font, or a new layout this script doesn't
  recognise) is reported and left for a human, exactly like it was the first time.
- **Idempotent.** Re-running this against an unchanged source must produce a byte-identical
  file, or a scheduled workflow would open an empty pull request every month. `fetched_at` is
  therefore only bumped when a record was actually added.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

if __package__ is None and __name__ == "__main__":  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import ClientSession, ClientTimeout

TERMS_URL: Final = "https://octopusenergy.co.jp/terms"
DEFAULT_DATA_PATH: Final = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "octopus_energy_japan"
    / "data"
    / "adder_baseline.json"
)
SCHEMA_VERSION: Final = 1

# Same fixed row order as `docs/ADDER_BASELINE.md` and `docs/TOU_SCHEMES.md`.
AREA_CODES: Final = ("01", "02", "03", "04", "05", "06", "07", "08", "09")
# Only used to double-check row order against the newer, labelled PDF layout when a month
# happens to use it — the older, unlabelled layout still relies on `AREA_CODES` order alone.
AREA_NAMES_JA: Final = ("北海道", "東北", "東京", "中部", "北陸", "関西", "中国", "四国", "九州")
JST_OFFSET: Final = timedelta(hours=9)

FUEL_LINK_RE: Final = re.compile(
    r"https://a\.storyblok\.com/f/122730/x/[a-z0-9]+/20\d{2}-\d{2}\.pdf"
)
LEVY_LINK_RE: Final = re.compile(r"https://a\.storyblok\.com/f/122730/x/[a-z0-9]+/fy20\d{2}_?\.pdf")
_NUMBER_RE: Final = re.compile(r"-?\d+(?:\.\d+)?")
_FUEL_MONTH_RE: Final = re.compile(r"/(20\d{2})-(\d{2})\.pdf$")
_LEVY_YEAR_RE: Final = re.compile(r"/fy(20\d{2})_?\.pdf$")

_REQUEST_TIMEOUT: Final = ClientTimeout(total=30)


class BaselineRefreshError(Exception):
    """The refresh could not proceed, and no file should be written."""


def jst_month_bounds_utc(year: int, month: int) -> tuple[str, str]:
    """Return the UTC `[valid_from, valid_to)` window for one JST calendar month."""
    start_jst = datetime(year, month, 1, tzinfo=UTC)
    end_jst = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    return _iso_utc(start_jst - JST_OFFSET), _iso_utc(end_jst - JST_OFFSET)


def fiscal_year_bounds_utc(fiscal_year: int) -> tuple[str, str]:
    """Return the UTC window for one levy fiscal year (JST May 1 through the next April 30)."""
    start_jst = datetime(fiscal_year, 5, 1, tzinfo=UTC)
    end_jst = datetime(fiscal_year + 1, 5, 1, tzinfo=UTC)
    return _iso_utc(start_jst - JST_OFFSET), _iso_utc(end_jst - JST_OFFSET)


def _iso_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def extract_fuel_values(pdf_bytes: bytes) -> list[Decimal] | None:
    """Return the nine areas' values in fixed `AREA_CODES` order, or None if unparseable.

    Handles both observed PDF layouts (bare-number rows, and `"北海道電力エリア", "3.88
    円/kWh"`-style rows) by pulling the numeric part out of whichever cell holds it, rather
    than assuming the cell is the whole value. When a row's first cell names its area — the
    newer layout does this, the older one does not — that name is checked against the expected
    `AREA_NAMES_JA` position rather than trusted blindly: a table that names its own rows and
    disagrees with the fixed order is a parsing bug, not data to ship.
    """
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = pdf.pages[0].extract_tables()
    if not tables or len(tables[0]) != len(AREA_CODES):
        return None
    values: list[Decimal] = []
    for index, row in enumerate(tables[0]):
        label = row[0]
        if label and label.strip() and AREA_NAMES_JA[index] not in label:
            return None
        cell = next((c for c in reversed(row) if c and c.strip()), None)
        if cell is None:
            return None
        match = _NUMBER_RE.search(cell)
        if match is None:
            return None
        try:
            values.append(Decimal(match.group(0)))
        except InvalidOperation:
            return None
    return values


def extract_levy_value(pdf_bytes: bytes) -> Decimal | None:
    """Return the single nationwide levy figure, or None if unparseable."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    match = re.search(r"([\d.]+)\s*円", text)
    if match is None:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


@dataclass(slots=True)
class RefreshResult:
    """What a refresh found, whether anything changed, and what could not be read."""

    payload: dict[str, Any]
    added_fuel_months: list[str] = field(default_factory=list)
    added_levy_years: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added_fuel_months or self.added_levy_years)


async def _fetch_text(session: ClientSession, url: str) -> str:
    async with session.get(url, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        return await response.text()


async def _fetch_bytes(session: ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        return await response.read()


async def refresh(existing: dict[str, Any], *, session: ClientSession) -> RefreshResult:
    """Return the existing baseline with any newly published months folded in.

    Every window already on disk is carried forward untouched and is never re-fetched — only a
    `(valid_from, valid_to)` this file has nothing for triggers a download.
    """
    html = await _fetch_text(session, TERMS_URL)
    fuel_links = sorted(set(FUEL_LINK_RE.findall(html)))
    levy_links = sorted(set(LEVY_LINK_RE.findall(html)))

    fuel_by_area: dict[str, list[dict[str, str]]] = {
        code: list(existing.get("fuel_cost_adjustment", {}).get(code, [])) for code in AREA_CODES
    }
    known_fuel_windows = {
        code: {(record["valid_from"], record["valid_to"]) for record in fuel_by_area[code]}
        for code in AREA_CODES
    }
    added_fuel_months: list[str] = []
    skipped: list[str] = []

    for url in fuel_links:
        match = _FUEL_MONTH_RE.search(url)
        if match is None:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        valid_from, valid_to = jst_month_bounds_utc(year, month)
        if all((valid_from, valid_to) in known_fuel_windows[code] for code in AREA_CODES):
            continue
        pdf_bytes = await _fetch_bytes(session, url)
        values = extract_fuel_values(pdf_bytes)
        if values is None:
            skipped.append(f"fuel cost adjustment {year}-{month:02d}: PDF did not parse ({url})")
            continue
        for code, value in zip(AREA_CODES, values, strict=True):
            if (valid_from, valid_to) in known_fuel_windows[code]:
                continue
            fuel_by_area[code].append(
                {
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "price_inc_tax": str(value),
                    "source_url": url,
                }
            )
            known_fuel_windows[code].add((valid_from, valid_to))
        added_fuel_months.append(f"{year}-{month:02d}")

    levy_records = list(existing.get("renewable_energy_levy", []))
    known_levy_windows = {(record["valid_from"], record["valid_to"]) for record in levy_records}
    added_levy_years: list[int] = []

    for url in levy_links:
        match = _LEVY_YEAR_RE.search(url)
        if match is None:
            continue
        fiscal_year = int(match.group(1))
        valid_from, valid_to = fiscal_year_bounds_utc(fiscal_year)
        if (valid_from, valid_to) in known_levy_windows:
            continue
        pdf_bytes = await _fetch_bytes(session, url)
        levy_value = extract_levy_value(pdf_bytes)
        if levy_value is None:
            skipped.append(f"renewable energy levy fy{fiscal_year}: PDF did not parse ({url})")
            continue
        levy_records.append(
            {
                "valid_from": valid_from,
                "valid_to": valid_to,
                "price_inc_tax": str(levy_value),
                "source_url": url,
            }
        )
        known_levy_windows.add((valid_from, valid_to))
        added_levy_years.append(fiscal_year)

    for code in AREA_CODES:
        fuel_by_area[code].sort(key=lambda record: record["valid_from"])
    levy_records.sort(key=lambda record: record["valid_from"])

    # Built key-by-key in the shipped file's own order (`schema_version`, `source`,
    # `fetched_at`, ...) rather than `dict(existing) | {...}`, so a run that only adds one
    # month reorders nothing else — the diff a reviewer sees is exactly the new month, not
    # every key in the file.
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": TERMS_URL,
        "fetched_at": existing.get("fetched_at"),
        "renewable_energy_levy": levy_records,
        "fuel_cost_adjustment": fuel_by_area,
        **{
            key: value
            for key, value in existing.items()
            if key
            not in {
                "schema_version",
                "source",
                "fetched_at",
                "renewable_energy_levy",
                "fuel_cost_adjustment",
            }
        },
    }

    result = RefreshResult(
        payload=payload,
        added_fuel_months=added_fuel_months,
        added_levy_years=added_levy_years,
        skipped=skipped,
    )
    if result.changed:
        payload["fetched_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return result


def _assert_monotonic(existing: dict[str, Any], result: RefreshResult) -> None:
    """Refuse to write a file that holds fewer records than what is already committed."""
    for code in AREA_CODES:
        before = len(existing.get("fuel_cost_adjustment", {}).get(code, []))
        after = len(result.payload["fuel_cost_adjustment"][code])
        if after < before:
            raise BaselineRefreshError(
                f"Refusing to write: area {code} would drop from {before} to {after} months"
            )
    before_levy = len(existing.get("renewable_energy_levy", []))
    after_levy = len(result.payload["renewable_energy_levy"])
    if after_levy < before_levy:
        raise BaselineRefreshError(
            f"Refusing to write: the levy table would drop from {before_levy} to {after_levy} years"
        )


def _write(data_path: Path, payload: dict[str, Any]) -> None:
    data_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _fetch_refresh(existing: dict[str, Any]) -> RefreshResult:
    async with ClientSession() as session:
        return await refresh(existing, session=session)


def main() -> None:
    """Refresh the shipped adder baseline in place, or report what would change."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-path",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help="path to adder_baseline.json (default: the shipped copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing the file",
    )
    args = parser.parse_args()

    existing = json.loads(args.data_path.read_text(encoding="utf-8"))
    result = asyncio.run(_fetch_refresh(existing))
    _assert_monotonic(existing, result)
    if result.changed and not args.dry_run:
        _write(args.data_path, result.payload)

    if result.added_fuel_months:
        print(f"Added fuel cost adjustment months: {', '.join(result.added_fuel_months)}")
    if result.added_levy_years:
        print(
            f"Added renewable energy levy fiscal years: {', '.join(map(str, result.added_levy_years))}"
        )
    if result.skipped:
        print("Could not parse (needs a human, see docs/ADDER_BASELINE.md):")
        for entry in result.skipped:
            print(f"  - {entry}")
    if not result.changed:
        print("No new data on the source page; nothing to do.")
    elif args.dry_run:
        print("Dry run: file left unchanged.")
    else:
        print(f"Wrote {args.data_path}")


if __name__ == "__main__":
    main()
