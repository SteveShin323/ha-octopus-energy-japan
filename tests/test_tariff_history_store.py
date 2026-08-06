"""Tests for the durable archive of rate adjustments.

The one that matters most is `test_a_payload_that_failed_to_load_is_never_written_over`.
Everything else here costs a re-fetch if it is wrong; that one costs the only copy of values
Octopus Energy Japan does not serve again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.octopus_energy_japan.api.tariff import (
    SupplyPointTariff,
    TariffAdder,
    TariffStep,
)
from custom_components.octopus_energy_japan.const import DOMAIN
from custom_components.octopus_energy_japan.tariff_history import (
    TARIFF_HISTORY_SCHEMA_VERSION,
    AdderKind,
)
from custom_components.octopus_energy_japan.tariff_history_store import (
    TariffHistoryArchive,
    TariffHistoryStore,
    storage_key,
)
from homeassistant.core import HomeAssistant

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
AUGUST = (
    datetime(2026, 7, 31, 15, tzinfo=UTC),
    datetime(2026, 8, 31, 15, tzinfo=UTC),
)
ENTRY_ID = "01JABCDEF"
SECRET = "11" * 32
SCOPE = f"supply-point-{'a' * 64}"


def _tariff(price: str = "4.32") -> SupplyPointTariff:
    return SupplyPointTariff(
        account_number="A-1",
        supply_point_id="SP-1",
        product_code="P",
        product_name="P",
        steps=(TariffStep(Decimal(0), None, Decimal("20.62")),),
        standing_charge_per_day=None,
        fuel_cost_adjustment=TariffAdder(
            price_inc_tax=Decimal(price),
            valid_from=AUGUST[0],
            valid_to=AUGUST[1],
        ),
        renewable_energy_levy=None,
    )


def _seed(hass_storage: dict[str, Any], key: str, payload: object) -> None:
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": payload}


@pytest.mark.parametrize(
    ("entry_id", "scope"),
    [
        ("../escape", SCOPE),
        (ENTRY_ID, "supply-point-short"),
        (ENTRY_ID, "A-REAL-ACCOUNT-NUMBER"),
    ],
)
def test_an_unsafe_key_is_refused_before_it_reaches_a_filename(
    entry_id: str,
    scope: str,
) -> None:
    with pytest.raises(ValueError, match="Tariff history"):
        storage_key(entry_id, scope)


def test_the_key_names_the_entry_and_an_opaque_identity() -> None:
    assert storage_key(ENTRY_ID, SCOPE) == f"{DOMAIN}.tariff_history.{ENTRY_ID}.{SCOPE}"


async def test_an_archive_round_trips_through_a_real_store(hass: HomeAssistant) -> None:
    store = TariffHistoryStore(hass, ENTRY_ID, SCOPE, save_delay=0)
    await store.async_load()
    archive = TariffHistoryArchive(hass, ENTRY_ID, SECRET, save_delay=0)

    await archive.async_observe([_tariff()], observed_at=NOW)
    await archive.async_flush()

    reopened = TariffHistoryArchive(hass, ENTRY_ID, SECRET, save_delay=0)
    await reopened.async_observe([_tariff()], observed_at=NOW)
    (record,) = reopened.schedule("A-1", "SP-1").records
    assert record.kind is AdderKind.FUEL_COST_ADJUSTMENT
    assert record.price_inc_tax == Decimal("4.32")
    assert reopened.archived_records == 1
    assert reopened.quarantined_supply_points == 0


async def test_a_payload_that_failed_to_load_is_never_written_over(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """The provider does not serve a past adjustment again, so a save would destroy it.

    The ledger recovers from a corrupt partition by loading an empty one and saving over it,
    which costs a re-fetch. Doing that here costs the data itself, so this store quarantines
    instead and the file is left exactly as it was.
    """
    key = storage_key(ENTRY_ID, SCOPE)
    unreadable = {"schema_version": TARIFF_HISTORY_SCHEMA_VERSION, "adders": "not-a-list"}
    _seed(hass_storage, key, unreadable)
    before = json.dumps(hass_storage[key], sort_keys=True)

    store = TariffHistoryStore(hass, ENTRY_ID, SCOPE, save_delay=0)
    await store.async_load()

    assert store.quarantined is True
    assert store.schedule.records == ()

    # Everything a refresh would do next must leave the stored payload alone.
    store.async_replace(())
    await store.async_flush()
    await hass.async_block_till_done()
    assert json.dumps(hass_storage[key], sort_keys=True) == before


async def test_a_store_that_cannot_be_read_at_all_also_quarantines(
    hass: HomeAssistant,
) -> None:
    store = TariffHistoryStore(hass, ENTRY_ID, SCOPE, save_delay=0)

    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        AsyncMock(side_effect=OSError("unreadable")),
    ):
        await store.async_load()

    assert store.quarantined is True


async def test_a_quarantined_supply_point_is_counted_for_the_repair_message(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    _seed(
        hass_storage,
        storage_key(ENTRY_ID, SCOPE),
        {"schema_version": TARIFF_HISTORY_SCHEMA_VERSION, "adders": "not-a-list"},
    )
    archive = TariffHistoryArchive(hass, ENTRY_ID, SECRET, save_delay=0)

    with patch(
        "custom_components.octopus_energy_japan.tariff_history_store.stable_supply_point_identity",
        return_value=SCOPE,
    ):
        await archive.async_observe([_tariff()], observed_at=NOW)

    assert archive.quarantined_supply_points == 1
    # Pricing continues from the live tariff rather than from an archive it must not trust.
    assert archive.schedule("A-1", "SP-1").records == ()


async def test_a_restated_price_is_counted_as_a_revision(hass: HomeAssistant) -> None:
    archive = TariffHistoryArchive(hass, ENTRY_ID, SECRET, save_delay=0)

    await archive.async_observe([_tariff("4.32")], observed_at=NOW)
    await archive.async_observe([_tariff("5.00")], observed_at=NOW)

    assert archive.revised_windows == 1
    assert archive.schedule("A-1", "SP-1").records[0].price_inc_tax == Decimal("5.00")


async def test_an_unknown_supply_point_has_an_empty_schedule(hass: HomeAssistant) -> None:
    archive = TariffHistoryArchive(hass, ENTRY_ID, SECRET, save_delay=0)

    assert archive.schedule("A-1", "SP-1").records == ()


def test_a_negative_save_delay_is_refused(hass: HomeAssistant) -> None:
    with pytest.raises(ValueError, match="save_delay"):
        TariffHistoryStore(hass, ENTRY_ID, SCOPE, save_delay=-1)


async def test_loading_twice_does_not_re_read_the_store(hass: HomeAssistant) -> None:
    """A refresh calls this on every observation; only the first should touch storage."""
    store = TariffHistoryStore(hass, ENTRY_ID, SCOPE, save_delay=0)
    await store.async_load()

    with patch(
        "homeassistant.helpers.storage.Store.async_load",
        AsyncMock(side_effect=AssertionError("the store must be read once")),
    ):
        await store.async_load()

    assert store.quarantined is False
