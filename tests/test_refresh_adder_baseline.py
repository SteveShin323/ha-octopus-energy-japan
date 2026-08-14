"""Tests for the adder baseline refresh script.

Network access and PDF parsing are both mocked here — this only exercises the merge logic
(never delete a window, only bump `fetched_at` when something actually changed) that
`docs/ADDER_BASELINE.md` and the module docstring promise. `extract_fuel_values` and
`extract_levy_value` are exercised separately against a mocked `pdfplumber`.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession
from scripts.refresh_adder_baseline import (
    AREA_CODES,
    AREA_NAMES_JA,
    BaselineRefreshError,
    RefreshResult,
    _assert_monotonic,
    extract_fuel_values,
    extract_levy_value,
    fiscal_year_bounds_utc,
    jst_month_bounds_utc,
    main,
    refresh,
)

TERMS_HTML = """
<html><body>
<a href="https://a.storyblok.com/f/122730/x/aaa111/2026-01.pdf">2026年1月分</a>
<a href="https://a.storyblok.com/f/122730/x/bbb222/2026-02.pdf">2026年2月分</a>
<a href="https://a.storyblok.com/f/122730/x/ccc333/fy2026_.pdf">FY2026</a>
</body></html>
"""


class _FakeResponse:
    def __init__(self, *, text: str = "", content: bytes = b"") -> None:
        self._text = text
        self._content = content

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._content


class _FakeSession:
    """Maps a fixed set of URLs to canned responses, exactly like OEJP's real disclosure page."""

    def __init__(self, pages: dict[str, str | bytes]) -> None:
        self._pages = pages

    def get(self, url: str, timeout: object = None) -> _FakeResponse:
        content = self._pages[url]
        if isinstance(content, bytes):
            return _FakeResponse(content=content)
        return _FakeResponse(text=content)


def _existing(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "source": "https://octopusenergy.co.jp/terms",
        "fetched_at": "2026-01-01T00:00:00Z",
        "renewable_energy_levy": [],
        "fuel_cost_adjustment": {code: [] for code in AREA_CODES},
    }
    base.update(overrides)
    return base


def test_jst_month_bounds_match_the_shipped_baseline() -> None:
    assert jst_month_bounds_utc(2026, 1) == ("2025-12-31T15:00:00Z", "2026-01-31T15:00:00Z")
    assert jst_month_bounds_utc(2026, 12) == ("2026-11-30T15:00:00Z", "2026-12-31T15:00:00Z")


def test_fiscal_year_bounds_span_may_to_the_following_may() -> None:
    assert fiscal_year_bounds_utc(2026) == ("2026-04-30T15:00:00Z", "2027-04-30T15:00:00Z")


def test_extract_fuel_values_reads_a_bare_number_table() -> None:
    """The older layout leaves the row's first cell blank; only the value is text."""
    fake_pdf = _fake_pdf([["", str(n)] for n in range(1, 10)])
    with patch("pdfplumber.open", return_value=fake_pdf):
        values = extract_fuel_values(b"irrelevant")

    assert values == [Decimal(str(n)) for n in range(1, 10)]


def test_extract_fuel_values_reads_a_labelled_table() -> None:
    fake_pdf = _fake_pdf(
        [[f"{name}電力エリア", f"{n}.50 円/kWh"] for n, name in enumerate(AREA_NAMES_JA, start=1)]
    )
    with patch("pdfplumber.open", return_value=fake_pdf):
        values = extract_fuel_values(b"irrelevant")

    assert values == [Decimal(f"{n}.50") for n in range(1, 10)]


def test_extract_fuel_values_rejects_a_mislabelled_row() -> None:
    """A table that names its own rows and disagrees with the fixed order is a parsing bug."""
    rows = [
        [f"{name}電力エリア", f"{n}.50 円/kWh"] for n, name in enumerate(AREA_NAMES_JA, start=1)
    ]
    rows[0], rows[1] = rows[1], rows[0]  # swap the first two areas' labels
    fake_pdf = _fake_pdf(rows)
    with patch("pdfplumber.open", return_value=fake_pdf):
        assert extract_fuel_values(b"irrelevant") is None


def test_extract_fuel_values_rejects_a_table_missing_an_area() -> None:
    fake_pdf = _fake_pdf([["", str(n)] for n in range(1, 9)])  # only eight rows
    with patch("pdfplumber.open", return_value=fake_pdf):
        assert extract_fuel_values(b"irrelevant") is None


def test_extract_levy_value_reads_the_nationwide_figure() -> None:
    fake_pdf = _fake_pdf_text("2026年5月分から2027年4月分まで単価は4.18円/kWhです。")
    with patch("pdfplumber.open", return_value=fake_pdf):
        assert extract_levy_value(b"irrelevant") == Decimal("4.18")


def _fake_pdf(table: list[list[str]]) -> Any:
    class _Page:
        def extract_tables(self) -> list[list[list[str]]]:
            return [table]

    class _Pdf:
        pages = [_Page()]

        def __enter__(self) -> _Pdf:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    return _Pdf()


def _fake_pdf_text(text: str) -> Any:
    class _Page:
        def extract_text(self) -> str:
            return text

    class _Pdf:
        pages = [_Page()]

        def __enter__(self) -> _Pdf:
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

    return _Pdf()


async def test_refresh_adds_a_new_month_without_touching_existing_windows() -> None:
    existing = _existing(
        fuel_cost_adjustment={
            code: [
                {
                    "valid_from": "2025-12-31T15:00:00Z",
                    "valid_to": "2026-01-31T15:00:00Z",
                    "price_inc_tax": "1.00",
                    "source_url": "https://a.storyblok.com/f/122730/x/aaa111/2026-01.pdf",
                }
            ]
            for code in AREA_CODES
        }
    )
    session = _FakeSession({"https://octopusenergy.co.jp/terms": TERMS_HTML})

    with (
        patch(
            "scripts.refresh_adder_baseline.TERMS_URL",
            "https://octopusenergy.co.jp/terms",
        ),
        patch(
            "scripts.refresh_adder_baseline._fetch_bytes",
            return_value=b"pdf-bytes",
        ),
        patch(
            "scripts.refresh_adder_baseline.extract_fuel_values",
            return_value=[Decimal("2.00")] * 9,
        ),
        patch(
            "scripts.refresh_adder_baseline.extract_levy_value",
            return_value=Decimal("4.18"),
        ),
    ):
        result = await refresh(existing, session=cast("ClientSession", session))

    assert result.changed
    assert result.added_fuel_months == ["2026-02"]
    assert result.added_levy_years == [2026]
    # The 2026-01 window from `existing` survives untouched, in every area.
    for code in AREA_CODES:
        windows = {
            (r["valid_from"], r["valid_to"]) for r in result.payload["fuel_cost_adjustment"][code]
        }
        assert ("2025-12-31T15:00:00Z", "2026-01-31T15:00:00Z") in windows
        assert ("2026-01-31T15:00:00Z", "2026-02-28T15:00:00Z") in windows
        assert len(result.payload["fuel_cost_adjustment"][code]) == 2


async def test_refresh_is_idempotent_when_every_window_is_already_known() -> None:
    existing = _existing(
        fuel_cost_adjustment={
            code: [
                {
                    "valid_from": "2025-12-31T15:00:00Z",
                    "valid_to": "2026-01-31T15:00:00Z",
                    "price_inc_tax": "1.00",
                    "source_url": "x",
                },
                {
                    "valid_from": "2026-01-31T15:00:00Z",
                    "valid_to": "2026-02-28T15:00:00Z",
                    "price_inc_tax": "1.50",
                    "source_url": "x",
                },
            ]
            for code in AREA_CODES
        },
        renewable_energy_levy=[
            {
                "valid_from": "2026-04-30T15:00:00Z",
                "valid_to": "2027-04-30T15:00:00Z",
                "price_inc_tax": "4.18",
                "source_url": "x",
            }
        ],
    )
    session = _FakeSession({"https://octopusenergy.co.jp/terms": TERMS_HTML})

    with patch("scripts.refresh_adder_baseline._fetch_bytes") as fetch_bytes:
        result = await refresh(existing, session=cast("ClientSession", session))

    assert not result.changed
    assert result.added_fuel_months == []
    assert result.added_levy_years == []
    fetch_bytes.assert_not_awaited()
    assert result.payload["fetched_at"] == "2026-01-01T00:00:00Z"


async def test_refresh_reports_a_pdf_it_cannot_parse_instead_of_dropping_the_month() -> None:
    existing = _existing()
    session = _FakeSession({"https://octopusenergy.co.jp/terms": TERMS_HTML})

    with (
        patch("scripts.refresh_adder_baseline._fetch_bytes", return_value=b"broken"),
        patch("scripts.refresh_adder_baseline.extract_fuel_values", return_value=None),
        patch("scripts.refresh_adder_baseline.extract_levy_value", return_value=None),
    ):
        result = await refresh(existing, session=cast("ClientSession", session))

    assert not result.changed
    assert any("2026-01" in entry for entry in result.skipped)
    assert any("fy2026" in entry for entry in result.skipped)
    for code in AREA_CODES:
        assert result.payload["fuel_cost_adjustment"][code] == []


def test_assert_monotonic_refuses_a_shrinking_area() -> None:
    existing = _existing(
        fuel_cost_adjustment={
            code: [{"valid_from": "a", "valid_to": "b", "price_inc_tax": "1", "source_url": "x"}]
            for code in AREA_CODES
        }
    )
    shrunk = json.loads(json.dumps(existing))
    shrunk["fuel_cost_adjustment"]["03"] = []
    result = RefreshResult(payload=shrunk)

    with pytest.raises(BaselineRefreshError, match="area 03"):
        _assert_monotonic(existing, result)


def test_assert_monotonic_refuses_a_shrinking_levy_table() -> None:
    existing = _existing(
        renewable_energy_levy=[
            {"valid_from": "a", "valid_to": "b", "price_inc_tax": "1", "source_url": "x"}
        ]
    )
    shrunk = json.loads(json.dumps(existing))
    shrunk["renewable_energy_levy"] = []
    result = RefreshResult(payload=shrunk)

    with pytest.raises(BaselineRefreshError, match="levy table"):
        _assert_monotonic(existing, result)


def test_assert_monotonic_allows_growth() -> None:
    existing = _existing()
    grown = json.loads(json.dumps(existing))
    grown["fuel_cost_adjustment"]["01"].append(
        {"valid_from": "a", "valid_to": "b", "price_inc_tax": "1", "source_url": "x"}
    )
    result = RefreshResult(payload=grown)

    _assert_monotonic(existing, result)  # does not raise


def test_main_writes_only_when_something_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_path = tmp_path / "adder_baseline.json"
    data_path.write_text(json.dumps(_existing()), encoding="utf-8")
    written_before = data_path.stat().st_mtime_ns

    unchanged_result = RefreshResult(payload=_existing())
    with (
        patch("sys.argv", ["refresh_adder_baseline.py", "--data-path", str(data_path)]),
        patch(
            "scripts.refresh_adder_baseline._fetch_refresh",
            AsyncMock(return_value=unchanged_result),
        ),
    ):
        main()

    assert data_path.stat().st_mtime_ns == written_before
    assert "nothing to do" in capsys.readouterr().out


def test_main_dry_run_never_writes(tmp_path: Path) -> None:
    data_path = tmp_path / "adder_baseline.json"
    original = _existing()
    data_path.write_text(json.dumps(original), encoding="utf-8")

    changed_payload = _existing()
    changed_payload["fuel_cost_adjustment"]["01"].append(
        {"valid_from": "a", "valid_to": "b", "price_inc_tax": "1", "source_url": "x"}
    )
    changed_result = RefreshResult(
        payload=changed_payload,
        added_fuel_months=["2099-01"],
    )
    with (
        patch(
            "sys.argv",
            ["refresh_adder_baseline.py", "--data-path", str(data_path), "--dry-run"],
        ),
        patch(
            "scripts.refresh_adder_baseline._fetch_refresh", AsyncMock(return_value=changed_result)
        ),
    ):
        main()

    assert json.loads(data_path.read_text(encoding="utf-8")) == original


def test_main_writes_the_regenerated_payload_when_something_changed(tmp_path: Path) -> None:
    data_path = tmp_path / "adder_baseline.json"
    data_path.write_text(json.dumps(_existing()), encoding="utf-8")

    changed_payload = _existing()
    changed_payload["fuel_cost_adjustment"]["01"].append(
        {"valid_from": "a", "valid_to": "b", "price_inc_tax": "1", "source_url": "x"}
    )
    changed_result = RefreshResult(
        payload=changed_payload,
        added_fuel_months=["2099-01"],
    )
    with (
        patch("sys.argv", ["refresh_adder_baseline.py", "--data-path", str(data_path)]),
        patch(
            "scripts.refresh_adder_baseline._fetch_refresh", AsyncMock(return_value=changed_result)
        ),
    ):
        main()

    assert json.loads(data_path.read_text(encoding="utf-8")) == changed_payload
