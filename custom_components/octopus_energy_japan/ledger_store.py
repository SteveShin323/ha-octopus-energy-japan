"""Private Home Assistant Store backend for monthly ledger partitions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .ledger import LedgerBackend, validate_partition_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_STORE_VERSION = 1
_DEFAULT_SAVE_DELAY = 15.0
_ENTRY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STORAGE_SCOPE_PATTERN = re.compile(r"supply-point-[0-9a-f]{64}")


class HomeAssistantLedgerBackend(LedgerBackend):
    """Persist private ledger payloads with debounced atomic HA Store writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        storage_scope: str,
        *,
        save_delay: float = _DEFAULT_SAVE_DELAY,
    ) -> None:
        if _ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
            raise ValueError("Ledger entry_id contains unsafe characters")
        if _STORAGE_SCOPE_PATTERN.fullmatch(storage_scope) is None:
            raise ValueError("Ledger storage_scope must be an opaque supply-point identity")
        if save_delay < 0:
            raise ValueError("Ledger save_delay must not be negative")
        self._hass = hass
        self._entry_id = entry_id
        self._storage_scope = storage_scope
        self._save_delay = save_delay
        self._index_store = Store[dict[str, Any]](
            hass,
            _STORE_VERSION,
            f"{DOMAIN}.ledger.{entry_id}.{storage_scope}.index",
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )
        self._partition_stores: dict[str, Store[dict[str, Any]]] = {}
        self._pending_partitions: dict[str, dict[str, Any]] = {}
        self._pending_index: dict[str, Any] | None = None

    async def async_load_index(self) -> set[str]:
        """Load and strictly validate the partition index."""
        payload = self._pending_index
        if payload is None:
            payload = await self._index_store.async_load()
        if payload is None:
            return set()
        if not isinstance(payload, dict):
            raise ValueError("Ledger partition index is not an object")
        raw_partitions = payload.get("partitions")
        if not isinstance(raw_partitions, list):
            raise ValueError("Ledger partition index is malformed")
        partitions: set[str] = set()
        for value in raw_partitions:
            if not isinstance(value, str):
                raise ValueError("Ledger partition index contains a non-string")
            validate_partition_id(value)
            partitions.add(value)
        return partitions

    async def async_save_index(self, partitions: set[str]) -> None:
        """Debounce an index update while retaining the newest snapshot."""
        for partition_id in partitions:
            validate_partition_id(partition_id)
        payload = {"partitions": sorted(partitions)}
        self._pending_index = payload
        if self._save_delay == 0:
            await self._index_store.async_save(payload)
            self._pending_index = None
            return
        self._index_store.async_delay_save(
            lambda: payload,
            self._save_delay,
        )

    async def async_load_partition(self, partition_id: str) -> Mapping[str, Any] | None:
        """Load one private monthly partition."""
        pending = self._pending_partitions.get(partition_id)
        if pending is not None:
            return pending
        return cast(
            "Mapping[str, Any] | None",
            await self._partition_store(partition_id).async_load(),
        )

    async def async_save_partition(
        self,
        partition_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Debounce one partition update while retaining the newest snapshot."""
        validate_partition_id(partition_id)
        serializable = deepcopy(dict(payload))
        self._pending_partitions[partition_id] = serializable
        store = self._partition_store(partition_id)
        if self._save_delay == 0:
            await store.async_save(serializable)
            self._pending_partitions.pop(partition_id, None)
            return
        store.async_delay_save(
            lambda: serializable,
            self._save_delay,
        )

    async def async_remove_partition(self, partition_id: str) -> None:
        """Remove one private partition and any pending replacement."""
        self._pending_partitions.pop(partition_id, None)
        await self._partition_store(partition_id).async_remove()
        self._partition_stores.pop(partition_id, None)

    async def async_flush(self) -> None:
        """Persist all pending payloads immediately for unload and tests."""
        pending_partitions = tuple(self._pending_partitions.items())
        for partition_id, payload in pending_partitions:
            await self._partition_store(partition_id).async_save(payload)
            if self._pending_partitions.get(partition_id) is payload:
                self._pending_partitions.pop(partition_id, None)
        if self._pending_index is not None:
            payload = self._pending_index
            await self._index_store.async_save(payload)
            if self._pending_index is payload:
                self._pending_index = None

    def _partition_store(self, partition_id: str) -> Store[dict[str, Any]]:
        validate_partition_id(partition_id)
        store = self._partition_stores.get(partition_id)
        if store is None:
            store = Store[dict[str, Any]](
                self._hass,
                _STORE_VERSION,
                (f"{DOMAIN}.ledger.{self._entry_id}.{self._storage_scope}.{partition_id}"),
                private=True,
                atomic_writes=True,
                serialize_in_event_loop=False,
            )
            self._partition_stores[partition_id] = store
        return store
