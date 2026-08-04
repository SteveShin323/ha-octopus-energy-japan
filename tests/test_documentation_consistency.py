"""Documentation must describe the sign-in methods the code actually offers.

Every stale claim this file guards against was real. The repository has said, at
various points, that the integration could not be connected at all, that the
email/password login existed only in a local probe, and that nothing is typed during
setup. Each was true when written and false by the time a user read it.

A prose audit catches those once. These checks catch them every run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from custom_components.octopus_energy_japan.const import (
    AUTH_METHOD_DEVICE,
    AUTH_METHOD_OAUTH,
    AUTH_METHOD_PASSWORD,
    AUTH_METHODS,
    OAUTH_AUTH_METHODS,
)

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "octopus_energy_japan"
TRANSLATIONS = ("strings.json", "translations/en.json", "translations/ja.json")


def _config(name: str) -> dict:
    payload = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))
    config = payload["config"]
    assert isinstance(config, dict)
    return config


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_the_method_menu_offers_exactly_the_implemented_methods(name: str) -> None:
    options = _config(name)["step"]["user"]["menu_options"]

    assert set(options) == set(AUTH_METHODS)
    assert all(text.strip() for text in options.values())


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_every_method_that_asks_for_input_has_a_translated_step(name: str) -> None:
    """A menu entry leading to an untranslated form shows the user a raw key."""
    steps = _config(name)["step"]

    for method in (AUTH_METHOD_PASSWORD, AUTH_METHOD_DEVICE):
        assert method in steps, f"{method} has no step text"
        assert steps[method].get("title", "").strip()
        assert steps[method].get("description", "").strip()


@pytest.mark.parametrize("name", TRANSLATIONS)
def test_the_device_flow_progress_message_carries_both_placeholders(name: str) -> None:
    """The code and the URL are the whole content of that screen."""
    message = _config(name)["progress"]["wait_for_device"]

    assert "{user_code}" in message
    assert "{url}" in message


def test_oauth_methods_are_the_ones_that_use_an_oauth_session() -> None:
    """`OAUTH_AUTH_METHODS` is what `__init__.py` routes and revokes with.

    Setup rejects a method in neither group, and removal revokes only for a method in
    this one, so the two constants have to stay complementary.
    """
    assert set(OAUTH_AUTH_METHODS) == set(AUTH_METHODS) - {AUTH_METHOD_PASSWORD}
    assert AUTH_METHOD_OAUTH in OAUTH_AUTH_METHODS
    assert AUTH_METHOD_DEVICE in OAUTH_AUTH_METHODS


@pytest.mark.parametrize(
    "document",
    [
        "docs/ja/README.md",
        "PRIVACY.md",
        "README.md",
    ],
)
def test_user_facing_documents_do_not_claim_the_integration_cannot_be_connected(
    document: str,
) -> None:
    """One method works today, so no user-facing page may say none does."""
    text = (ROOT / document).read_text(encoding="utf-8")

    for claim in (
        "cannot be connected",
        "You cannot connect it yet",
        "Nothing is typed during setup",
        "There is nothing to type during setup",
    ):
        assert claim not in text, f"{document} still says {claim!r}"


def test_the_password_method_is_documented_wherever_privacy_is_described() -> None:
    """Storing a credential is the one thing a reader must not have to discover."""
    privacy = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    guide = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "stored in Home Assistant" in privacy
    assert "0008-password-authentication.md" in privacy
    assert "stored in Home Assistant" in guide


def test_the_quality_scale_records_only_the_known_outstanding_rule() -> None:
    """A `todo` that nobody notices is how a release ships an unmet claim."""
    rules = yaml.safe_load((INTEGRATION / "quality_scale.yaml").read_text(encoding="utf-8"))
    outstanding = {
        name
        for name, value in rules["rules"].items()
        if (value if isinstance(value, str) else value.get("status")) == "todo"
    }

    # `brands` needs the domain to be public in home-assistant/brands first.
    assert outstanding == {"brands"}
