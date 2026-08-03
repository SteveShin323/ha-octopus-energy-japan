"""Manifest regression tests for optional Home Assistant component use."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def test_recorder_is_ordered_but_not_a_hard_dependency() -> None:
    manifest = json.loads(
        Path("custom_components/octopus_energy_japan/manifest.json").read_text(encoding="utf-8")
    )
    assert isinstance(manifest, dict)
    typed_manifest: dict[str, Any] = manifest

    assert "recorder" in typed_manifest["after_dependencies"]
    assert "recorder" not in typed_manifest["dependencies"]
