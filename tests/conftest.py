"""Shared Home Assistant fixtures for the OEJP integration tests."""

from __future__ import annotations

from contextlib import suppress

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request: pytest.FixtureRequest) -> None:
    """Enable custom integrations when the Home Assistant plugin is installed."""
    if request.node.get_closest_marker("recorder_harness") is not None:
        # Home Assistant imports Recorder only for type checking in this module,
        # while the test plugin autospec evaluates annotations under Python 3.14.
        from homeassistant.components.recorder import Recorder, migration
        from homeassistant.helpers import recorder as recorder_helper
        from sqlalchemy.orm.session import Session

        migration.Recorder = Recorder
        recorder_helper.Recorder = Recorder
        recorder_helper.Session = Session
        return
    with suppress(pytest.FixtureLookupError):
        request.getfixturevalue("enable_custom_integrations")
