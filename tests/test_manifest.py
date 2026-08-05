"""Manifest regression tests for optional Home Assistant component use."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest
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


def test_the_brand_images_are_where_hacs_looks_for_them() -> None:
    """HACS reads these two from inside the component, and fails the build without them.

    Home Assistant itself serves brand images from `brands.home-assistant.io`, which makes an
    in-component copy look redundant. It is not, and moving them out breaks three separate
    jobs — measured on a throwaway branch rather than reasoned about:

    - `hacs` warns "does not contain brands assets at
      custom_components/octopus_energy_japan/brand/icon.png. Falling back to checking the
      brands repository", then fails with "does not provide brand assets and is not listed in
      the Home Assistant brands repository". This integration is not listed there yet.
    - `links` fails, because the README and the Japanese guide show the logo from this path.
    - `python` fails at `pip install -e .`, with "Multiple top-level packages discovered in a
      flat-layout: ['brand', 'custom_components']". A sibling directory of
      `custom_components` breaks the package build outright.

    The third has nothing to do with HACS and is the one worth knowing: a top-level `brand/`
    is not merely redundant, it is unbuildable.
    """
    brand = Path("custom_components/octopus_energy_japan/brand")

    assert (brand / "icon.png").is_file()
    assert (brand / "logo.png").is_file()


# What `home-assistant/brands` requires. A submission with a wrong size is rejected, and
# the only place that is discovered is the pull request, so it is checked here instead.
BRAND = Path("custom_components/octopus_energy_japan/brand")
BRAND_IMAGES = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "logo.png": (None, 240),
    "logo@2x.png": (None, 480),
    "dark_logo.png": (None, 240),
    "dark_logo@2x.png": (None, 480),
}


def _png_header(path: Path) -> tuple[int, int, int]:
    """Return width, height, and PNG colour type without a third-party decoder."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[25]


@pytest.mark.parametrize(("name", "expected"), sorted(BRAND_IMAGES.items()))
def test_every_brand_image_matches_what_the_brands_repository_accepts(
    name: str,
    expected: tuple[int | None, int],
) -> None:
    path = BRAND / name
    assert path.is_file(), name

    width, height, colour_type = _png_header(path)
    expected_width, expected_height = expected

    assert height == expected_height, f"{name} is {width}x{height}"
    if expected_width is not None:
        assert width == expected_width, f"{name} is {width}x{height}"
    # Colour type 6 is RGBA. A brand image on an opaque background shows a box in the
    # Home Assistant integration list, in whichever theme it was not drawn for.
    assert colour_type == 6, f"{name} has no alpha channel"


def test_the_two_logo_widths_are_consistent_with_each_other() -> None:
    """A 2x asset that is not twice the size renders blurred rather than sharp."""
    for base in ("logo", "dark_logo"):
        single_width, single_height, _ = _png_header(BRAND / f"{base}.png")
        double_width, double_height, _ = _png_header(BRAND / f"{base}@2x.png")

        assert double_height == single_height * 2
        # Odd single widths cannot double exactly, so one pixel of rounding is allowed.
        assert abs(double_width - single_width * 2) <= 1


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
