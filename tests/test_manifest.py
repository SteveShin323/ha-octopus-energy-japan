"""Manifest regression tests for optional Home Assistant component use."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml


def test_recorder_is_ordered_but_not_a_hard_dependency() -> None:
    manifest = json.loads(
        Path("custom_components/octopus_energy_japan/manifest.json").read_text(encoding="utf-8")
    )
    assert isinstance(manifest, dict)
    typed_manifest: dict[str, Any] = manifest

    assert "recorder" in typed_manifest["after_dependencies"]
    assert "recorder" not in typed_manifest["dependencies"]


def _manifest() -> dict[str, Any]:
    manifest = json.loads(
        Path("custom_components/octopus_energy_japan/manifest.json").read_text(encoding="utf-8")
    )
    assert isinstance(manifest, dict)
    return manifest


def test_manifest_and_package_versions_agree() -> None:
    """A release is tagged from one version; two sources must not disagree."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"$', pyproject, re.M)

    assert declared is not None
    assert _manifest()["version"] == declared.group(1)


def test_manifest_points_at_documentation_and_the_issue_tracker() -> None:
    manifest = _manifest()

    assert manifest["documentation"].startswith("https://")
    assert manifest["issue_tracker"].startswith("https://")
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["config_flow"] is True
    # A read-only cloud integration needs no third-party package.
    assert manifest["requirements"] == []


def test_shipped_component_contains_every_required_file() -> None:
    """HACS installs this directory verbatim, so a missing file breaks a release."""
    component = Path("custom_components/octopus_energy_japan")

    for required in (
        "manifest.json",
        "strings.json",
        "icons.json",
        "quality_scale.yaml",
        "translations/en.json",
        "translations/ja.json",
        "brand/icon.png",
        "brand/logo.png",
        "diagnostics.py",
        "issues.py",
    ):
        assert (component / required).is_file(), required


def test_only_brands_remains_outstanding_in_the_quality_scale() -> None:
    """Every other rule must be met or exempt with a stated reason."""
    scale = yaml.safe_load(
        (Path("custom_components/octopus_energy_japan/quality_scale.yaml")).read_text(
            encoding="utf-8"
        )
    )
    rules: dict[str, Any] = scale["rules"]

    outstanding = {
        name
        for name, value in rules.items()
        if (value if isinstance(value, str) else value["status"]) == "todo"
    }
    assert outstanding == {"brands"}

    for name, value in rules.items():
        if isinstance(value, dict) and value["status"] in {"exempt", "todo"}:
            assert value.get("comment"), name
