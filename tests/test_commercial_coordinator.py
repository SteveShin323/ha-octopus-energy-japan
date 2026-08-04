"""Tests for the low-cadence optional commercial coordinator."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api import (
    AccountCommercialSnapshot,
    AccountOverview,
    AuthenticatedGraphQLClient,
    CommercialAvailability,
    CommercialFeature,
    GraphQLErrorDetail,
    OejpAccount,
    OejpAuthenticationError,
    OejpTransportError,
)
from custom_components.octopus_energy_japan.commercial_coordinator import (
    OejpCommercialCoordinator,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _snapshot(account_id: str) -> AccountCommercialSnapshot:
    return AccountCommercialSnapshot(
        account_id,
        overview=AccountOverview(account_id, "ACTIVE", 100, 0, True, False),
        observed_at=NOW,
    )


def _coordinator(
    hass: HomeAssistant,
    accounts: tuple[OejpAccount, ...],
) -> OejpCommercialCoordinator:
    return OejpCommercialCoordinator(
        hass,
        MockConfigEntry(),
        AsyncMock(spec=AuthenticatedGraphQLClient),
        accounts,
        now=lambda: NOW,
    )


async def test_commercial_coordinator_fetches_every_discovered_account(
    hass: HomeAssistant,
) -> None:
    accounts = (OejpAccount("B"), OejpAccount("A"))
    coordinator = _coordinator(hass, accounts)
    fetch = AsyncMock(side_effect=lambda _client, account_id, **_kwargs: _snapshot(account_id))

    with patch(
        "custom_components.octopus_energy_japan.commercial_coordinator.async_fetch_account_commercial_snapshot",
        fetch,
    ):
        data = await coordinator._async_update_data()

    assert [snapshot.account_id for snapshot in data.accounts] == ["B", "A"]
    assert data.observed_at == NOW
    assert [call.args[1] for call in fetch.await_args_list] == ["B", "A"]
    assert all(call.kwargs == {"observed_at": NOW} for call in fetch.await_args_list)


async def test_commercial_failure_preserves_last_values_and_marks_access_failed(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, (OejpAccount("A"),))
    coordinator.data = coordinator.data.__class__((_snapshot("A"),), NOW)

    with patch(
        "custom_components.octopus_energy_japan.commercial_coordinator.async_fetch_account_commercial_snapshot",
        AsyncMock(side_effect=OejpTransportError("offline")),
    ):
        data = await coordinator._async_update_data()

    snapshot = data.account("A")
    assert snapshot is not None and snapshot.overview is not None
    assert snapshot.overview.balance_minor == 100
    assert {snapshot.feature_access(feature).availability for feature in CommercialFeature} == {
        CommercialAvailability.FAILED
    }


async def test_first_commercial_failure_reports_no_values_instead_of_guessing(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, (OejpAccount("A"),))

    with patch(
        "custom_components.octopus_energy_japan.commercial_coordinator.async_fetch_account_commercial_snapshot",
        AsyncMock(side_effect=OejpTransportError("offline")),
    ):
        data = await coordinator._async_update_data()

    snapshot = data.account("A")
    assert snapshot is not None
    assert snapshot.overview is None
    assert snapshot.agreements == ()
    assert snapshot.latest_bill is None
    assert snapshot.latest_transaction is None
    assert {snapshot.feature_access(feature).availability for feature in CommercialFeature} == {
        CommercialAvailability.FAILED
    }


async def test_commercial_coordinator_rejects_naive_timestamps(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        OejpCommercialCoordinator(
            hass,
            MockConfigEntry(),
            AsyncMock(spec=AuthenticatedGraphQLClient),
            (OejpAccount("A"),),
            now=lambda: datetime(2026, 8, 3, 12),  # noqa: DTZ001
        )


async def test_commercial_authentication_failure_requests_reauth(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, (OejpAccount("A"),))
    error = OejpAuthenticationError((GraphQLErrorDetail("safe", error_type="AUTHENTICATION"),))

    with (
        patch(
            "custom_components.octopus_energy_japan.commercial_coordinator.async_fetch_account_commercial_snapshot",
            AsyncMock(side_effect=error),
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()


async def test_commercial_roster_changes_take_effect_on_next_refresh(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, (OejpAccount("A"),))
    coordinator.set_accounts((OejpAccount("C"),))

    with patch(
        "custom_components.octopus_energy_japan.commercial_coordinator.async_fetch_account_commercial_snapshot",
        AsyncMock(return_value=_snapshot("C")),
    ):
        data = await coordinator._async_update_data()

    assert [snapshot.account_id for snapshot in data.accounts] == ["C"]
