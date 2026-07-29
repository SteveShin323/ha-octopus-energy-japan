"""Shared Home Assistant fixtures for the OEJP integration tests."""

from __future__ import annotations

from contextlib import suppress

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Enable custom integrations when the Home Assistant plugin is installed."""
    with suppress(pytest.FixtureLookupError):
        request.getfixturevalue("enable_custom_integrations")
