"""Tests for the local OEJP probe command."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api.operations import OejpToken
from scripts.oejp_probe import (
    ACCOUNT_NUMBER_ENV,
    OPERATIONS,
    SUPPLY_POINT_ENV,
    ProbeContext,
    _authorization_header,
    _write_fixture,
    build_context,
)

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)


def _context(**environment: str) -> ProbeContext:
    with patch.dict("os.environ", environment, clear=True):
        return build_context(hours=48, now=NOW)


def test_probe_exposes_only_fixed_query_operations() -> None:
    assert set(OPERATIONS) == {
        "account_agreements",
        "account_billing",
        "account_overview",
        "generic_devices",
        "generic_export_readings",
        "generic_import_readings",
        "legacy_half_hourly_readings",
        "legacy_interval_readings",
        "resource_discovery",
        "schema_capabilities",
        "viewer_accounts",
        "viewer_identity",
    }
    for operation in OPERATIONS.values():
        normalized = operation.query.casefold()
        assert "query " in normalized
        assert "mutation " not in normalized
        assert "subscription " not in normalized


def test_probe_window_is_bounded_and_ends_now() -> None:
    context = _context()

    assert context.end_at == NOW
    assert (context.end_at - context.start_at).total_seconds() == 48 * 3600
    assert context.graphql_end() == "2026-08-04T12:00:00Z"

    with pytest.raises(ValueError, match="at least one hour"):
        build_context(hours=0, now=NOW)


def test_probe_reads_local_only_targets_from_the_environment() -> None:
    context = _context(**{ACCOUNT_NUMBER_ENV: "PRIVATE-ACCOUNT", SUPPLY_POINT_ENV: "PRIVATE-SPIN"})

    assert context.account() == "PRIVATE-ACCOUNT"
    assert context.supply_point() == "PRIVATE-SPIN"


def test_probe_explains_which_target_is_missing() -> None:
    context = _context()

    with pytest.raises(RuntimeError, match=ACCOUNT_NUMBER_ENV):
        context.account()
    with pytest.raises(RuntimeError, match=SUPPLY_POINT_ENV):
        context.supply_point()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("account_overview", {"accountNumber"}),
        ("account_agreements", {"accountNumber", "after"}),
        ("account_billing", {"accountNumber"}),
        ("legacy_half_hourly_readings", {"accountNumber", "fromDatetime", "toDatetime"}),
        ("legacy_interval_readings", {"accountNumber", "startAt", "endAt"}),
        ("generic_devices", {"externalIdentifier", "marketName"}),
        (
            "generic_import_readings",
            {
                "externalIdentifier",
                "marketName",
                "startAt",
                "endAt",
                "units",
                "first",
                "after",
            },
        ),
    ],
)
def test_probe_operations_bind_only_declared_variables(name: str, expected: set[str]) -> None:
    operation = OPERATIONS[name]
    context = _context(**{ACCOUNT_NUMBER_ENV: "PRIVATE-ACCOUNT", SUPPLY_POINT_ENV: "PRIVATE-SPIN"})

    assert operation.variables is not None
    variables = operation.variables(context)
    assert set(variables) == expected
    for declared in expected:
        assert f"${declared}" in operation.query


def test_fixed_discovery_operations_take_no_variables() -> None:
    for name in ("viewer_identity", "viewer_accounts", "resource_discovery", "schema_capabilities"):
        assert OPERATIONS[name].variables is None


def test_probe_refuses_to_overwrite_fixture_without_explicit_force(
    tmp_path: Path,
) -> None:
    output = tmp_path / "fixture.json"
    fixture: dict[str, object] = {"safe": True}
    _write_fixture(output, fixture, force=False)

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        _write_fixture(output, fixture, force=False)

    replacement: dict[str, object] = {"safe": False}
    _write_fixture(output, replacement, force=True)
    assert '"safe": false' in output.read_text(encoding="utf-8")


async def test_probe_prefers_complete_authorization_header_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OEJP_AUTHORIZATION_HEADER", "Bearer local-token")
    monkeypatch.setenv("OEJP_EMAIL", "unused@example.jp")
    monkeypatch.setenv("OEJP_PASSWORD", "unused")

    assert await _authorization_header(AsyncMock()) == "Bearer local-token"


async def test_probe_legacy_login_is_isolated_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OEJP_AUTHORIZATION_HEADER", raising=False)
    monkeypatch.setenv("OEJP_EMAIL", "local@example.jp")
    monkeypatch.setenv("OEJP_PASSWORD", "local-password")
    obtain = AsyncMock(return_value=OejpToken(access_token="legacy-access"))

    with patch("scripts.oejp_probe.async_obtain_token", obtain):
        assert await _authorization_header(AsyncMock()) == "JWT legacy-access"

    obtain.assert_awaited_once()


async def test_probe_refuses_to_run_without_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OEJP_AUTHORIZATION_HEADER", raising=False)
    monkeypatch.delenv("OEJP_EMAIL", raising=False)
    monkeypatch.delenv("OEJP_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="OEJP_AUTHORIZATION_HEADER"):
        await _authorization_header(AsyncMock())
