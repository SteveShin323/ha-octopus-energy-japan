"""Tests that diagnostics stay useful and never leak customer data."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock

from custom_components.octopus_energy_japan.aggregation import (
    AggregationSnapshot,
    PeriodAggregate,
    SupplyPointAggregation,
)
from custom_components.octopus_energy_japan.api import (
    AccountCommercialSnapshot,
    AccountOverview,
    BillSummary,
    Capability,
    CapabilityAvailability,
    CapabilitySnapshot,
    CommercialAccess,
    CommercialAvailability,
    CommercialFeature,
    OejpAccount,
    OejpProperty,
    OejpSupplyPoint,
    ReadingDirection,
    ReadingFallbackReason,
    ReadingProviderName,
    ResourceLifecycle,
)
from custom_components.octopus_energy_japan.commercial_coordinator import OejpCommercialData
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import (
    DirectionSyncStatus,
    OejpCoordinatorData,
    ProviderObservation,
)
from custom_components.octopus_energy_japan.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.octopus_energy_japan.runtime import OejpRuntimeData
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
SECRET = "01" * 32

# Every value a real installation must never publish.
ACCOUNT_NUMBER = "A-B71D14A3"
SPIN = "03-0011-1000-1274-4432-3211"
SUPPLY_POINT_ID = "PRIVATE-SUPPLY-POINT"
EMAIL = "private@example.jp"
ACCESS_TOKEN = "private-access-token-value"
ADDRESS = "000-0000 PRIVATE-ADDRESS"
SECRETS = (ACCOUNT_NUMBER, SPIN, SUPPLY_POINT_ID, EMAIL, ACCESS_TOKEN, SECRET, ADDRESS)


def _accounts() -> tuple[OejpAccount, ...]:
    return (
        OejpAccount(
            number=ACCOUNT_NUMBER,
            lifecycle=ResourceLifecycle.ACTIVE,
            properties=(
                OejpProperty(
                    id="PRIVATE-PROPERTY",
                    address=ADDRESS,
                    postcode="000-0000",
                    supply_points=(
                        OejpSupplyPoint(
                            id=SUPPLY_POINT_ID,
                            spin=SPIN,
                            account_number=ACCOUNT_NUMBER,
                            direction=ReadingDirection.IMPORT,
                            lifecycle=ResourceLifecycle.ACTIVE,
                        ),
                        OejpSupplyPoint(
                            id="PRIVATE-OLD-POINT",
                            spin="03-0000-0000-0000-0000-0000",
                            account_number=ACCOUNT_NUMBER,
                            lifecycle=ResourceLifecycle.HISTORICAL,
                        ),
                    ),
                ),
            ),
        ),
    )


def _coordinator_data() -> OejpCoordinatorData:
    aggregation = SupplyPointAggregation(
        account_id=ACCOUNT_NUMBER,
        supply_point_id=SUPPLY_POINT_ID,
        direction=ReadingDirection.IMPORT,
        latest=None,
        today=PeriodAggregate(Decimal("1.25"), interval_count=48, complete=True),
        yesterday=PeriodAggregate(Decimal("4.5"), complete=True),
        this_week=PeriodAggregate(Decimal("12.75"), complete=True),
        this_month=PeriodAggregate(Decimal("48.25"), interval_count=1440, complete=True),
        last_month=PeriodAggregate(Decimal("120.5"), interval_count=1488, complete=True),
        latest_reading_end=NOW - timedelta(minutes=30),
        data_delay=timedelta(minutes=90),
    )
    return OejpCoordinatorData(
        accounts=_accounts(),
        capabilities=CapabilitySnapshot().replace(
            (Capability.GENERIC_READINGS,),
            CapabilityAvailability.SUPPORTED,
            "introspected",
        ),
        aggregation=AggregationSnapshot((aggregation,), NOW),
        present_supply_points=frozenset({(ACCOUNT_NUMBER, SUPPLY_POINT_ID)}),
        enabled_supply_points=frozenset({(ACCOUNT_NUMBER, SUPPLY_POINT_ID)}),
        direction_statuses=(
            DirectionSyncStatus(
                account_identity="account-hmac",
                supply_point_identity="point-hmac",
                direction=ReadingDirection.IMPORT,
                queryable=True,
                last_success_at=NOW,
                coverage_start_at=NOW - timedelta(days=30),
                coverage_end_at=NOW,
            ),
        ),
        provider_observations=(
            ProviderObservation(
                account_identity="account-hmac",
                supply_point_identity="point-hmac",
                direction=ReadingDirection.IMPORT,
                provider=ReadingProviderName.LEGACY,
                fallback_reason=ReadingFallbackReason.GENERIC_CAPABILITY_UNSUPPORTED,
                observed_at=NOW,
            ),
        ),
        correction_count=3,
        last_refresh_change_count=5,
        corrupt_partition_count=1,
    )


def _entry(hass: HomeAssistant, *, loaded: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_implementation": "local",
            "token": {"access_token": ACCESS_TOKEN, "refresh_token": "private-refresh"},
        },
        options={"enabled_historical_resources": ["point-hmac"]},
    )
    entry.add_to_hass(hass)
    if not loaded:
        entry.runtime_data = None
        return entry

    coordinator = Mock()
    coordinator.data = _coordinator_data()
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.update_interval = timedelta(minutes=30)

    commercial = Mock()
    commercial.last_update_success = True
    commercial.data = OejpCommercialData(
        (
            AccountCommercialSnapshot(
                ACCOUNT_NUMBER,
                overview=AccountOverview(ACCOUNT_NUMBER, "ACTIVE", 1234, 0, True, False),
                latest_bill=BillSummary(
                    id="bill",
                    type_name="PeriodBasedDocumentType",
                    bill_type="STATEMENT",
                    from_date=None,
                    to_date=None,
                    issued_date=None,
                    due_date=None,
                    gross_amount_minor=13773,
                    status=None,
                    is_annulled=False,
                    is_held=False,
                ),
                access=(
                    CommercialAccess(
                        CommercialFeature.AGREEMENTS,
                        CommercialAvailability.FORBIDDEN,
                        error_codes=("KT-CT-1111",),
                        error_types=("AUTHORIZATION",),
                        error_paths=(("account", "marketSupplyAgreements"),),
                    ),
                ),
                observed_at=NOW,
            ),
        ),
        NOW,
    )

    entry.runtime_data = OejpRuntimeData(
        auth=AsyncMock(),
        accounts=_coordinator_data().accounts,
        capabilities=_coordinator_data().capabilities,
        identity_secret=SECRET,
        coordinator=coordinator,
        commercial_coordinator=commercial,
    )
    return entry


async def test_diagnostics_never_contain_a_customer_value(hass: HomeAssistant) -> None:
    report = await async_get_config_entry_diagnostics(hass, _entry(hass))

    serialized = json.dumps(report, default=str)
    for secret in SECRETS:
        assert secret not in serialized
    # A monetary amount is customer data even though it is only a number.
    assert "13773" not in serialized
    assert "1234" not in serialized


async def test_diagnostics_report_the_state_an_issue_report_needs(hass: HomeAssistant) -> None:
    report = await async_get_config_entry_diagnostics(hass, _entry(hass))

    assert report["integration"]["domain"] == DOMAIN
    assert report["integration"]["home_assistant"]
    assert report["integration"]["ledger_schema_version"] >= 1
    assert report["config_entry"]["has_stored_token"] is True
    assert report["config_entry"]["has_auth_implementation"] is True
    assert report["config_entry"]["selected_historical_resources"] == 1

    runtime: dict[str, Any] = report["runtime"]
    assert runtime["loaded"] is True
    assert runtime["last_update_success"] is True
    assert runtime["update_interval_seconds"] == 1800
    assert runtime["corrections"] == 3
    assert runtime["corrupt_partitions"] == 1
    assert runtime["resources"] == {
        "accounts": 1,
        "historical_accounts": 0,
        "supply_points": 2,
        "historical_supply_points": 1,
        "present_supply_points": 1,
        "enabled_supply_points": 1,
    }
    assert {status["capability"] for status in runtime["capabilities"]} == {
        Capability.GENERIC_READINGS.value
    }

    assert report["directions"][0]["direction"] == ReadingDirection.IMPORT.value
    assert report["directions"][0]["account"] == "account-hmac"
    assert report["providers"][0]["provider"] == ReadingProviderName.LEGACY.value
    assert report["providers"][0]["fallback_reason"] is not None
    assert report["aggregation"]["projections"] == 1
    assert report["aggregation"]["recent_intervals"] == 1440 + 1488
    assert report["aggregation"]["max_data_delay_seconds"] == 5400
    assert report["aggregation"]["timezone"] == "Asia/Tokyo"


async def test_diagnostics_expose_commercial_permission_without_values(
    hass: HomeAssistant,
) -> None:
    report = await async_get_config_entry_diagnostics(hass, _entry(hass))

    commercial = report["commercial"]
    assert commercial["configured"] is True
    account = commercial["accounts"][0]
    assert account["has_overview"] is True
    assert account["has_latest_bill"] is True
    assert account["agreements"] == 0
    feature = account["features"][0]
    assert feature["availability"] == CommercialAvailability.FORBIDDEN.value
    assert feature["error_codes"] == ["KT-CT-1111"]
    assert feature["error_paths"] == [["account", "marketSupplyAgreements"]]


async def test_diagnostics_work_before_the_runtime_is_loaded(hass: HomeAssistant) -> None:
    report = await async_get_config_entry_diagnostics(hass, _entry(hass, loaded=False))

    assert report["runtime"] == {"loaded": False}
    assert report["integration"]["domain"] == DOMAIN
    assert "directions" not in report


async def test_diagnostics_report_a_coordinator_exception_by_type_only(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    entry.runtime_data.coordinator.last_exception = TimeoutError("private detail")
    entry.runtime_data.coordinator.last_update_success = False

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["runtime"]["last_exception"] == "TimeoutError"
    assert "private detail" not in json.dumps(report, default=str)


async def test_diagnostics_count_a_historical_account(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    data = _coordinator_data()
    ended = OejpAccount(
        number="A-ENDED",
        lifecycle=ResourceLifecycle.HISTORICAL,
        properties=(),
    )
    entry.runtime_data.coordinator.data = replace(data, accounts=(*data.accounts, ended))

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["runtime"]["resources"]["accounts"] == 2
    assert report["runtime"]["resources"]["historical_accounts"] == 1


async def test_diagnostics_report_no_commercial_coordinator(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    entry.runtime_data.commercial_coordinator = None

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["commercial"] == {"configured": False}


async def test_diagnostics_report_capabilities_before_the_coordinator_exists(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    entry.runtime_data.coordinator = None

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["runtime"]["loaded"] is False
    assert {status["capability"] for status in report["runtime"]["capabilities"]} == {
        Capability.GENERIC_READINGS.value
    }
    assert "directions" not in report


async def test_diagnostics_omit_data_delay_when_no_projection_reports_one(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    data = _coordinator_data()
    projection = replace(data.aggregation.supply_points[0], data_delay=None)
    entry.runtime_data.coordinator.data = replace(
        data,
        aggregation=AggregationSnapshot((projection,), NOW),
    )

    report = await async_get_config_entry_diagnostics(hass, entry)

    assert report["aggregation"]["max_data_delay_seconds"] is None


async def test_diagnostics_never_contain_a_stored_credential(hass: HomeAssistant) -> None:
    """The password method stores a credential; diagnostics must not carry it.

    The report is built from an allow-list, so this cannot regress quietly — but the
    point of the diagnostics contract is that a user can attach the file to a public
    issue without reading it first, and a stored password would make that unsafe.
    """
    email = "person@example.test"
    password = "correct horse"
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            "auth_method": "password",
            "email": email,
            "password": password,
            "access_token": "legacy-access",
            "refresh_token": "legacy-refresh",
        },
    )

    report = await async_get_config_entry_diagnostics(hass, entry)

    serialised = json.dumps(report, default=str)
    for secret in (email, password, "legacy-access", "legacy-refresh"):
        assert secret not in serialised


async def test_diagnostics_do_not_carry_the_device_serial_numbers(
    hass: HomeAssistant,
) -> None:
    """The device page shows the account number and SPIN; the download must not.

    Those two exist so a customer can identify their own supply point in their own
    Home Assistant. The diagnostics file is meant to be attachable to a public issue
    without reading it, so the same values must not travel in it.
    """
    from custom_components.octopus_energy_japan.runtime import (
        async_project_discovered_devices,
    )
    from homeassistant.helpers import device_registry as dr

    entry = _entry(hass)
    runtime = entry.runtime_data
    assert isinstance(runtime, OejpRuntimeData)
    async_project_discovered_devices(hass, entry, runtime)

    serials = {
        device.serial_number
        for device in dr.async_get(hass).devices.values()
        if device.serial_number
    }
    assert serials, "the device page must carry an identifier for this test to mean anything"

    report = json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)

    for serial in serials:
        assert serial not in report
