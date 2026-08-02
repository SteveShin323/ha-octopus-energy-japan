"""English and Japanese Home Assistant translation completeness tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "octopus_energy_japan"


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    if isinstance(value, dict):
        return {path for key, child in value.items() for path in _leaf_paths(child, (*prefix, key))}
    return {prefix}


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_english_and_japanese_translations_match_canonical_strings() -> None:
    canonical = _leaf_paths(_load(INTEGRATION / "strings.json"))

    assert _leaf_paths(_load(INTEGRATION / "translations" / "en.json")) == canonical
    assert _leaf_paths(_load(INTEGRATION / "translations" / "ja.json")) == canonical
    assert not (INTEGRATION / "translations" / "ko.json").exists()
