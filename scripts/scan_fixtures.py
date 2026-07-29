#!/usr/bin/env python3
"""Fail when committed OEJP contract fixtures contain unsafe data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from custom_components.octopus_energy_japan.probe import (
    assert_contract_provenance,
    assert_safe_fixture,
)

DEFAULT_FIXTURE_ROOT = Path("tests/fixtures/contracts")


def scan_fixture(path: Path) -> None:
    """Load and validate one JSON contract fixture."""
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    assert_safe_fixture(payload)
    assert_contract_provenance(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Fixture files; defaults to tests/fixtures/contracts/**/*.json",
    )
    args = parser.parse_args()
    paths = args.paths or sorted(DEFAULT_FIXTURE_ROOT.rglob("*.json"))
    for path in paths:
        scan_fixture(path)


if __name__ == "__main__":
    main()
