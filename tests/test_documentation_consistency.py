"""Documentation must describe the sign-in methods the code actually offers.

Every stale claim this file guards against was real. The repository has said, at
various points, that the integration could not be connected at all, that the
email/password login existed only in a local probe, and that nothing is typed during
setup. Each was true when written and false by the time a user read it.

A prose audit catches those once. These checks catch them every run.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime, timedelta
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

    # Since Home Assistant 2026.3 the brand images ship inside the component, so the
    # `brands` rule is met by `brand/` rather than by a pull request elsewhere.
    assert not outstanding, outstanding


ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"


def test_every_module_the_architecture_table_names_exists() -> None:
    """A map that points at a file which moved is worse than no map."""
    table = ARCHITECTURE.read_text(encoding="utf-8")
    table = table.split("## Where the code lives")[1].split("**The dependency rule")[0]
    modules = sorted(set(re.findall(r"`([a-z_]+(?:/[a-z_]+)?\.py)`", table)))

    assert len(modules) > 25, f"the table stopped listing modules: {modules}"
    missing = [name for name in modules if not (INTEGRATION / name).is_file()]
    assert not missing, f"named in ARCHITECTURE.md but absent: {missing}"


def test_nothing_under_api_imports_home_assistant() -> None:
    """Invariant 7. `api/` is a standalone client, testable without Home Assistant.

    Stated in `ARCHITECTURE.md` as the one dependency rule that holds, so it is checked here
    rather than trusted. An import added anywhere under `api/` would make that claim false and
    couple the client to the framework.
    """
    offenders: list[str] = []
    for path in sorted((INTEGRATION / "api").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # `level > 1` is a parent-package import: `from .. import x`.
                if module.startswith("homeassistant") or (node.level or 0) > 1:
                    offenders.append(f"{path.name}: from {'.' * (node.level or 0)}{module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("homeassistant")
                )

    assert not offenders, f"api/ must not depend on Home Assistant: {offenders}"


def test_the_documented_refresh_intervals_are_the_ones_the_code_uses() -> None:
    """Both the README and `ARCHITECTURE.md` publish a cadence table.

    A reader plans around these numbers, and nothing else would fail if a constant changed.
    """
    from custom_components.octopus_energy_japan.commercial_coordinator import (
        COMMERCIAL_UPDATE_INTERVAL,
    )
    from custom_components.octopus_energy_japan.const import DEFAULT_SCAN_INTERVAL_MINUTES
    from custom_components.octopus_energy_japan.sync import (
        BILLING_INTERVAL,
        CONTRACT_INTERVAL,
        DISCOVERY_INTERVAL,
        POLL_OVERLAP,
    )

    assert DEFAULT_SCAN_INTERVAL_MINUTES == 30
    assert timedelta(hours=72) == POLL_OVERLAP
    assert timedelta(hours=24) == DISCOVERY_INTERVAL
    assert CONTRACT_INTERVAL == BILLING_INTERVAL == COMMERCIAL_UPDATE_INTERVAL
    assert timedelta(hours=12) == CONTRACT_INTERVAL

    for document in (ROOT / "README.md", ARCHITECTURE, ROOT / "docs" / "ja" / "README.md"):
        text = document.read_text(encoding="utf-8")
        assert "30" in text and "72" in text and "24" in text and "12" in text, document.name


def test_the_ledger_key_excludes_version_so_a_correction_replaces() -> None:
    """`ARCHITECTURE.md` says a re-published interval replaces the earlier one in place.

    That only holds while `version` stays out of the key. Adding it would store both versions
    side by side, and every total would double-count the corrected hour.
    """
    from custom_components.octopus_energy_japan.api.models import ReadingSeriesKey
    from custom_components.octopus_energy_japan.ledger import LedgerIntervalKey

    assert "version" not in ReadingSeriesKey.__dataclass_fields__
    assert "version" not in LedgerIntervalKey.__dataclass_fields__
    # The fields the document enumerates, so the prose and the dataclass stay in step.
    assert set(ReadingSeriesKey.__dataclass_fields__) == {
        "account_id",
        "supply_point_id",
        "direction",
        "unit",
        "source",
        "device_id",
        "register_id",
    }


def test_partitions_are_one_per_month_as_documented() -> None:
    from custom_components.octopus_energy_japan.ledger import partition_id_for

    assert partition_id_for(datetime(2026, 8, 5, 23, 30, tzinfo=UTC)) == "2026-08"
    assert partition_id_for(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == "2026-01"


def test_the_documented_availability_states_and_repair_issues_are_complete() -> None:
    """`ARCHITECTURE.md` enumerates both sets. An added member would go undocumented."""
    from custom_components.octopus_energy_japan.api import CommercialAvailability
    from custom_components.octopus_energy_japan.issues import OejpIssue

    assert {state.value for state in CommercialAvailability} == {
        "available",
        "partial",
        "forbidden",
        "unsupported",
        "failed",
    }
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    for state in CommercialAvailability:
        assert state.value in architecture, state

    assert len(list(OejpIssue)) == 4, [issue.value for issue in OejpIssue]
    assert not any("reauth" in issue.value for issue in OejpIssue), (
        "reauthentication is Home Assistant's own prompt, not a repair issue"
    )
