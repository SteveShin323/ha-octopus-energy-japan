"""Tests for the committed-fixture scanner command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from custom_components.octopus_energy_japan.api.operations import VIEWER_ACCOUNTS_QUERY
from custom_components.octopus_energy_japan.probe import UnsafeFixtureError, build_contract_fixture
from scripts.scan_fixtures import scan_fixture


def test_fixture_scanner_accepts_synthetic_json(tmp_path: Path) -> None:
    fixture = tmp_path / "safe.json"
    fixture.write_text(
        json.dumps(
            build_contract_fixture(
                "viewer_accounts",
                VIEWER_ACCOUNTS_QUERY,
                {"viewer": {"accounts": [{"number": "A-1"}]}},
            )
        ),
        encoding="utf-8",
    )

    scan_fixture(fixture)


def test_fixture_scanner_rejects_non_object_and_unsafe_json(tmp_path: Path) -> None:
    non_object = tmp_path / "array.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        scan_fixture(non_object)

    unsafe = tmp_path / "unsafe.json"
    unsafe.write_text('{"email":"customer@example.jp"}', encoding="utf-8")
    with pytest.raises(UnsafeFixtureError):
        scan_fixture(unsafe)
