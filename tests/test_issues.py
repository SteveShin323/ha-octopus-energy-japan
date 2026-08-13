"""Tests for repair issues raised from runtime state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from custom_components.octopus_energy_japan.aggregation import AggregationSnapshot
from custom_components.octopus_energy_japan.api import (
    AccountCommercialSnapshot,
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CommercialAccess,
    CommercialAvailability,
    CommercialFeature,
    ReadingDirection,
)
from custom_components.octopus_energy_japan.api.tariff import (
    SupplyPointTariff,
    TariffStep,
    TariffUnpriceable,
)
from custom_components.octopus_energy_japan.commercial_coordinator import OejpCommercialData
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import (
    DirectionSyncStatus,
    OejpCoordinatorData,
)
from custom_components.octopus_energy_japan.issues import (
    READING_SILENCE_THRESHOLD,
    OejpIssue,
    async_clear_issues,
    async_update_issues,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
ENTRY_ID = "entry"


def _data(**overrides: object) -> OejpCoordinatorData:
    base = OejpCoordinatorData(
        accounts=(),
        capabilities=CapabilitySnapshot(),
        aggregation=AggregationSnapshot((), NOW),
        present_supply_points=frozenset(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _direction(*, last_success_at: datetime | None, queryable: bool = True) -> DirectionSyncStatus:
    return DirectionSyncStatus(
        account_identity="account-hmac",
        supply_point_identity="point-hmac",
        direction=ReadingDirection.IMPORT,
        queryable=queryable,
        last_success_at=last_success_at,
    )


def _issue(hass: HomeAssistant, issue: OejpIssue) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ENTRY_ID}_{issue.value}")


def _update(hass: HomeAssistant, data: OejpCoordinatorData, commercial: object = None) -> None:
    async_update_issues(hass, ENTRY_ID, data, commercial, NOW)  # type: ignore[arg-type]


async def test_healthy_runtime_raises_no_issue(hass: HomeAssistant) -> None:
    _update(hass, _data(direction_statuses=(_direction(last_success_at=NOW),)))

    assert all(_issue(hass, issue) is None for issue in OejpIssue)


async def test_corrupt_partitions_are_reported_as_an_error(hass: HomeAssistant) -> None:
    _update(hass, _data(corrupt_partition_count=2))

    issue = _issue(hass, OejpIssue.LEDGER_PARTITIONS_CORRUPT)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.is_fixable is False
    assert issue.translation_placeholders == {"count": "2"}
    assert issue.learn_more_url is not None


async def test_a_silent_reading_series_is_reported_once_past_the_threshold(
    hass: HomeAssistant,
) -> None:
    inside = _direction(last_success_at=NOW - READING_SILENCE_THRESHOLD + timedelta(minutes=1))
    _update(hass, _data(direction_statuses=(inside,)))
    assert _issue(hass, OejpIssue.READINGS_SILENT) is None

    outside = _direction(last_success_at=NOW - READING_SILENCE_THRESHOLD - timedelta(minutes=1))
    _update(hass, _data(direction_statuses=(outside,)))
    issue = _issue(hass, OejpIssue.READINGS_SILENT)
    assert issue is not None
    assert issue.translation_placeholders == {"count": "1", "hours": "36"}


async def test_a_direction_that_never_succeeded_counts_as_silent(hass: HomeAssistant) -> None:
    _update(hass, _data(direction_statuses=(_direction(last_success_at=None),)))

    assert _issue(hass, OejpIssue.READINGS_SILENT) is not None


async def test_a_non_queryable_direction_is_not_reported_as_silent(hass: HomeAssistant) -> None:
    """A direction the provider does not expose is not a fault."""
    _update(
        hass,
        _data(direction_statuses=(_direction(last_success_at=None, queryable=False),)),
    )

    assert _issue(hass, OejpIssue.READINGS_SILENT) is None


async def test_only_reading_capabilities_are_reported(hass: HomeAssistant) -> None:
    topology_only = CapabilitySnapshot().replace(
        (Capability.DEVICES, Capability.REGISTERS),
        CapabilityAvailability.UNSUPPORTED,
        "generic_device_discovery_unavailable",
    )
    _update(hass, _data(capabilities=topology_only))
    assert _issue(hass, OejpIssue.CAPABILITY_UNAVAILABLE) is None

    readings_lost = topology_only.replace(
        (Capability.GENERIC_READINGS,),
        CapabilityAvailability.FORBIDDEN,
        "forbidden",
    )
    _update(hass, _data(capabilities=readings_lost))
    issue = _issue(hass, OejpIssue.CAPABILITY_UNAVAILABLE)
    assert issue is not None
    assert issue.translation_placeholders == {"capabilities": Capability.GENERIC_READINGS.value}


async def test_forbidden_commercial_features_are_reported(hass: HomeAssistant) -> None:
    commercial = OejpCommercialData(
        (
            AccountCommercialSnapshot(
                "ACCOUNT",
                access=(
                    CommercialAccess(
                        CommercialFeature.AGREEMENTS, CommercialAvailability.FORBIDDEN
                    ),
                    CommercialAccess(CommercialFeature.OVERVIEW, CommercialAvailability.AVAILABLE),
                ),
            ),
        ),
        NOW,
    )

    _update(hass, _data(), commercial)

    issue = _issue(hass, OejpIssue.COMMERCIAL_PERMISSION_MISSING)
    assert issue is not None
    assert issue.translation_placeholders == {"features": CommercialFeature.AGREEMENTS.value}


def _tariff(reason: TariffUnpriceable | None) -> SupplyPointTariff:
    return SupplyPointTariff(
        account_number="ACCOUNT",
        supply_point_id="POINT",
        product_code="P",
        product_name="P",
        steps=() if reason else (TariffStep(Decimal(0), None, Decimal("20.62")),),
        standing_charge_per_day=None,
        fuel_cost_adjustment=None,
        renewable_energy_levy=None,
        unpriceable_reason=reason,
    )


async def test_a_tariff_this_formula_cannot_price_is_reported(hass: HomeAssistant) -> None:
    """An absent cost statistic looks the same whether the plan or the integration is at fault.

    The user has no other way to learn that their plan shape, not a defect, is the reason.
    """
    commercial = OejpCommercialData(
        (), NOW, tariffs=(_tariff(TariffUnpriceable.TIME_OF_USE_SCHEME_UNKNOWN),)
    )

    _update(hass, _data(), commercial)

    issue = _issue(hass, OejpIssue.TARIFF_NOT_PRICEABLE)
    assert issue is not None
    assert issue.translation_placeholders == {"reasons": "time_of_use_scheme_unknown"}


async def test_a_priceable_tariff_raises_no_issue(hass: HomeAssistant) -> None:
    commercial = OejpCommercialData((), NOW, tariffs=(_tariff(None),))

    _update(hass, _data(), commercial)

    assert _issue(hass, OejpIssue.TARIFF_NOT_PRICEABLE) is None


async def test_a_supply_point_with_no_consumption_agreement_is_not_a_problem(
    hass: HomeAssistant,
) -> None:
    """An export-only supply point has no consumption cost, which is expected, not a fault."""
    _update(hass, _data(), OejpCommercialData((), NOW))

    assert _issue(hass, OejpIssue.TARIFF_NOT_PRICEABLE) is None


async def test_a_recovered_condition_clears_its_issue(hass: HomeAssistant) -> None:
    _update(hass, _data(corrupt_partition_count=1))
    assert _issue(hass, OejpIssue.LEDGER_PARTITIONS_CORRUPT) is not None

    _update(hass, _data(corrupt_partition_count=0))
    assert _issue(hass, OejpIssue.LEDGER_PARTITIONS_CORRUPT) is None


async def test_removing_an_entry_clears_every_issue(hass: HomeAssistant) -> None:
    _update(
        hass,
        _data(corrupt_partition_count=1, direction_statuses=(_direction(last_success_at=None),)),
    )
    assert _issue(hass, OejpIssue.LEDGER_PARTITIONS_CORRUPT) is not None

    async_clear_issues(hass, ENTRY_ID)

    assert all(_issue(hass, issue) is None for issue in OejpIssue)
