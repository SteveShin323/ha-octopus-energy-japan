"""Tests for the local OEJP probe command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api.operations import OejpToken
from scripts.oejp_probe import OPERATIONS, _authorization_header, _write_fixture


def test_probe_exposes_only_fixed_query_operations() -> None:
    assert set(OPERATIONS) == {"viewer_accounts", "viewer_identity"}
    for operation in OPERATIONS.values():
        normalized = operation.query.casefold()
        assert "query " in normalized
        assert "mutation " not in normalized


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
