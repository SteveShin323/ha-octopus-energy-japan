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


def _references(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(value, dict):
        return [path for key, child in value.items() for path in _references(child, (*prefix, key))]
    return [prefix] if isinstance(value, str) and "[%key:" in value else []


def test_no_translation_still_points_at_a_shared_string() -> None:
    """`[%key:...%]` is resolved by a build step this integration never runs.

    Home Assistant's own repository expands those references when it compiles
    `strings.json` into `translations/`. A custom integration ships its translations
    directly, so a reference left in one is displayed to the user verbatim — which is
    exactly what happened: the setup screen read
    `[%key:common::config_flow::create_entry::authenticated%]`.

    `strings.json` may keep them. It is the canonical source and is never displayed.
    """
    for language in ("en", "ja"):
        path = INTEGRATION / "translations" / f"{language}.json"
        unresolved = _references(_load(path))
        assert not unresolved, f"{language}.json still references: {sorted(unresolved)}"
