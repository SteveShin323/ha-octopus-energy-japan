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
from custom_components.octopus_energy_japan.api.tariff import (
    SupplyPointTariff,
    TariffAdder,
    TariffStep,
    TariffUnpriceable,
)
from custom_components.octopus_energy_japan.commercial_coordinator import OejpCommercialData
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.coordinator import (
    DirectionSyncStatus,
    HistoryGapSummary,
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

# Every value a real installation must never publish. Invented, and deliberately so: these
# are the needles the assertions below search the report for, and using a real account's
# number, supply-point number, product name, or bill amount would have put exactly what this
# test defends into a public repository.
ACCOUNT_NUMBER = "A-DEMO0001"
SPIN = "03-0000-0000-0000-0000-0000"
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
    coordinator.abandoned_gap_windows = Mock(
        return_value=[
            {
                "supply_point": "point-hmac",
                "direction": "import",
                "start_at": "2026-04-08T00:00:00+00:00",
                "end_at": "2026-04-15T00:00:00+00:00",
                "empty_attempts": 3,
                "last_attempt_at": "2026-08-13T00:00:00+00:00",
            }
        ]
    )
    coordinator.async_history_gaps = AsyncMock(
        return_value=(
            HistoryGapSummary(
                account="account-hmac",
                supply_point="point-hmac",
                direction=ReadingDirection.IMPORT,
                gaps=2,
                missing_hours=36.5,
                earliest_gap_at=datetime(2026, 4, 8, tzinfo=UTC),
                latest_gap_end_at=datetime(2026, 6, 10, tzinfo=UTC),
            ),
        )
    )

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
                    gross_amount_minor=84200,
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
        tariffs=(
            SupplyPointTariff(
                account_number=ACCOUNT_NUMBER,
                supply_point_id=SUPPLY_POINT_ID,
                product_code="JPN_KK_OCTOPUS_MAY_26",
                product_name="Demo Standard Plan",
                steps=(
                    TariffStep(Decimal(0), Decimal(120), Decimal("20.62")),
                    TariffStep(Decimal(120), None, Decimal("25.29")),
                ),
                standing_charge_per_day=Decimal("38.80"),
                fuel_cost_adjustment=TariffAdder(Decimal("4.32")),
                renewable_energy_levy=None,
                standing_charge_unit="YEN_AMPERE_DAY",
                product_type="ElectricitySteppedProduct",
            ),
            SupplyPointTariff(
                account_number=ACCOUNT_NUMBER,
                supply_point_id="OTHER-POINT",
                product_code="TOU",
                product_name="TOU",
                steps=(),
                standing_charge_per_day=None,
                fuel_cost_adjustment=None,
                renewable_energy_levy=None,
                product_type="ElectricitySteppedProduct",
                unpriceable_reason=TariffUnpriceable.TIME_OF_USE_SCHEME_UNKNOWN,
            ),
        ),
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
    assert "84200" not in serialized
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


async def test_diagnostics_report_the_tariff_shape_without_a_price(
    hass: HomeAssistant,
) -> None:
    """An absent cost statistic needs the plan's shape to be diagnosable.

    Whether a plan can be priced, what kind of product it is, how many steps and rate
    generations it has, and what the standing charge is measured in are all that distinguish
    an unsupported plan from a defect. None of them is a monetary amount.
    """
    report = await async_get_config_entry_diagnostics(hass, _entry(hass))

    priceable, unpriceable = report["tariffs"]
    assert priceable["priceable"] is True
    assert priceable["unpriceable_reason"] is None
    assert priceable["steps"] == 2
    assert priceable["rate_generations"] == 1
    assert priceable["standing_charge_unit"] == "YEN_AMPERE_DAY"
    assert priceable["has_standing_charge"] is True
    assert priceable["has_fuel_cost_adjustment"] is True
    assert priceable["has_renewable_energy_levy"] is False
    assert unpriceable["priceable"] is False
    assert unpriceable["unpriceable_reason"] == "time_of_use_scheme_unknown"

    # The supply point is named by its installation-local identity, never its provider id.
    serialized = json.dumps(report["tariffs"], default=str)
    assert SUPPLY_POINT_ID not in serialized
    assert ACCOUNT_NUMBER not in serialized
    for amount in ("20.62", "25.29", "38.80", "4.32"):
        assert amount not in serialized


async def test_diagnostics_report_when_a_tariff_is_an_estimate(hass: HomeAssistant) -> None:
    """`is_estimate` is what distinguishes a closed account's approximated cost from a live one's."""
    entry = _entry(hass)
    commercial = entry.runtime_data.commercial_coordinator
    data = commercial.data
    estimate = replace(data.tariffs[0], is_estimate=True)
    commercial.data = replace(data, tariffs=(estimate, data.tariffs[1]))

    report = await async_get_config_entry_diagnostics(hass, entry)

    priceable, unpriceable = report["tariffs"]
    assert priceable["is_estimate"] is True
    assert unpriceable["is_estimate"] is False


async def test_diagnostics_report_extrapolated_adder_hours(hass: HomeAssistant) -> None:
    """How many of an estimate's priced hours came from the nearest archived adder, not its own."""
    entry = _entry(hass)
    entry.runtime_data.coordinator.extrapolated_adder_hours = Mock(
        return_value=[{"supply_point": "point-hmac", "extrapolated_adder_hours": 12}]
    )

    report = await async_get_config_entry_diagnostics(hass, entry)

    (reported,) = report["extrapolated_adder_hours"]
    assert reported["extrapolated_adder_hours"] == 12
    assert ACCOUNT_NUMBER not in str(report["extrapolated_adder_hours"])


async def test_diagnostics_report_baseline_adder_hours(hass: HomeAssistant) -> None:
    """How many priced hours came from the shipped baseline rather than this account's own."""
    entry = _entry(hass)
    entry.runtime_data.coordinator.baseline_adder_hours = Mock(
        return_value=[{"supply_point": "point-hmac", "baseline_adder_hours": 7}]
    )

    report = await async_get_config_entry_diagnostics(hass, entry)

    (reported,) = report["baseline_adder_hours"]
    assert reported["baseline_adder_hours"] == 7
    assert ACCOUNT_NUMBER not in str(report["baseline_adder_hours"])


async def test_diagnostics_report_the_billing_anchor_and_what_said_so(
    hass: HomeAssistant,
) -> None:
    """The rule was measured on one account with one closed invoice.

    A user whose bill does not line up has to be able to say which evidence produced their
    anchor, and whether the provider's own reported reading day agrees with it.
    """
    report = await async_get_config_entry_diagnostics(hass, _entry(hass))

    periods = report["billing_periods"]
    # One per discovered supply point, including the ended one.
    assert len(periods) == 2
    for period in periods:
        assert period["source"] in {"reading_schedule", "supply_anchor", "calendar_month"}
        assert period["anchor_day"] is None or 1 <= period["anchor_day"] <= 31
        assert "reading_day_of_month" in period
        assert "agrees_with_reported_reading_day" in period
    assert SUPPLY_POINT_ID not in json.dumps(report["billing_periods"], default=str)


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


async def test_diagnostics_explain_a_history_that_stays_short(hass: HomeAssistant) -> None:
    """A stretch nobody can fill needs a reason, not just an absence."""
    entry = _entry(hass)

    report = await async_get_config_entry_diagnostics(hass, entry)

    (abandoned,) = report["abandoned_gap_windows"]
    assert abandoned["empty_attempts"] == 3
    assert abandoned["direction"] == "import"
    # Identities only, never the provider's own.
    assert ACCOUNT_NUMBER not in str(report["abandoned_gap_windows"])
