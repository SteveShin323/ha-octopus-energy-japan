"""Durable per-supply-point storage for the additions the API stops serving.

Follows the ledger's storage conventions — one private, atomically written Home Assistant
`Store` per supply point, keyed by an opaque identity rather than a provider number — with one
rule of its own, stated here because the surrounding precedent does the opposite:

**A file that failed to load is never written to.** The ledger recovers from a corrupt partition
by loading an empty one and saving over it, which costs a re-fetch. These values cannot be
re-fetched at any price, so a supply point whose archive cannot be read is put into read-only
quarantine: it keeps pricing from the live tariff, it is counted in diagnostics, and it raises a
repair issue, but its file is left exactly as it is. One save over a payload that merely failed
to parse would destroy the only copy.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .api.tariff import SupplyPointTariff
from .const import DOMAIN
from .identity import stable_supply_point_identity
from .tariff_history import (
    AdderSchedule,
    ArchivedAdder,
    TariffHistoryError,
    deserialize_adders,
    merge_observed,
    observed_adders,
    serialize_adders,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORE_VERSION = 1
_DEFAULT_SAVE_DELAY = 15.0
_ENTRY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STORAGE_SCOPE_PATTERN = re.compile(r"supply-point-[0-9a-f]{64}")

_LOGGER = logging.getLogger(__name__)


def storage_key(entry_id: str, storage_scope: str) -> str:
    """Return the store key one supply point's archive lives under."""
    if _ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
        raise ValueError("Tariff history entry_id contains unsafe characters")
    if _STORAGE_SCOPE_PATTERN.fullmatch(storage_scope) is None:
        raise ValueError("Tariff history storage_scope must be an opaque supply-point identity")
    return f"{DOMAIN}.tariff_history.{entry_id}.{storage_scope}"


class TariffHistoryStore:
    """One supply point's archive, loaded once and written only when it changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        storage_scope: str,
        *,
        save_delay: float = _DEFAULT_SAVE_DELAY,
    ) -> None:
        if save_delay < 0:
            raise ValueError("Tariff history save_delay must not be negative")
        self._store = Store[dict[str, Any]](
            hass,
            _STORE_VERSION,
            storage_key(entry_id, storage_scope),
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )
        self._save_delay = save_delay
        self._records: tuple[ArchivedAdder, ...] = ()
        self._loaded = False
        self._quarantined = False

    @property
    def quarantined(self) -> bool:
        """Report whether this archive failed to load and is therefore read-only."""
        return self._quarantined

    @property
    def schedule(self) -> AdderSchedule:
        """Return what has been archived, which is empty while quarantined."""
        return AdderSchedule(self._records)

    async def async_load(self) -> None:
        """Load once. A payload that cannot be read quarantines rather than being replaced."""
        if self._loaded or self._quarantined:
            return
        try:
            payload = await self._store.async_load()
        except OSError:
            self._quarantined = True
            _LOGGER.warning(
                "Unable to read the stored Octopus Energy Japan rate adjustments for one supply "
                "point. Past hours will be priced from the rate the provider reports now, and "
                "the stored file is left untouched"
            )
            return
        if payload is None:
            self._loaded = True
            return
        try:
            self._records = deserialize_adders(payload)
        except TariffHistoryError:
            self._quarantined = True
            _LOGGER.warning(
                "The stored Octopus Energy Japan rate adjustments for one supply point could not "
                "be read. Past hours will be priced from the rate the provider reports now. The "
                "file is not overwritten, because the provider does not serve those values again"
            )
            return
        self._loaded = True

    def async_replace(self, records: tuple[ArchivedAdder, ...]) -> None:
        """Persist a changed archive, unless this supply point is quarantined."""
        if self._quarantined:
            # The one rule this store exists to enforce.
            return
        self._records = records
        self._store.async_delay_save(lambda: serialize_adders(records), self._save_delay)

    async def async_flush(self) -> None:
        """Write any debounced save immediately."""
        if self._quarantined:
            return
        await self._store.async_save(serialize_adders(self._records))


class TariffHistoryArchive:
    """Every supply point's archive for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        identity_secret: str,
        *,
        save_delay: float = _DEFAULT_SAVE_DELAY,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._identity_secret = identity_secret
        self._save_delay = save_delay
        self._stores: dict[tuple[str, str], TariffHistoryStore] = {}

    async def async_observe(
        self,
        tariffs: Iterable[SupplyPointTariff],
        *,
        observed_at: datetime,
    ) -> None:
        """File what the provider is reporting now, writing only what changed."""
        for tariff in tariffs:
            scope = (tariff.account_number, tariff.supply_point_id)
            store = self._stores.get(scope)
            if store is None:
                store = TariffHistoryStore(
                    self._hass,
                    self._entry_id,
                    stable_supply_point_identity(self._identity_secret, *scope),
                    save_delay=self._save_delay,
                )
                self._stores[scope] = store
                await store.async_load()
            merged = merge_observed(
                store.schedule.records,
                observed_adders(tariff),
                observed_at=observed_at,
            )
            if merged is not None:
                store.async_replace(merged)

    def schedule(self, account_id: str, supply_point_id: str) -> AdderSchedule:
        """Return what has been archived for one supply point."""
        store = self._stores.get((account_id, supply_point_id))
        return store.schedule if store is not None else AdderSchedule()

    @property
    def quarantined_supply_points(self) -> int:
        """Return how many archives could not be read and are therefore read-only."""
        return sum(1 for store in self._stores.values() if store.quarantined)

    @property
    def archived_records(self) -> int:
        """Return how many periods have been archived across every supply point."""
        return sum(len(store.schedule.records) for store in self._stores.values())

    @property
    def revised_windows(self) -> int:
        """Return how many archived periods the provider later restated at a new price."""
        return sum(
            1
            for store in self._stores.values()
            for record in store.schedule.records
            if record.revisions
        )

    async def async_flush(self) -> None:
        """Write every debounced save immediately."""
        for store in self._stores.values():
            await store.async_flush()
