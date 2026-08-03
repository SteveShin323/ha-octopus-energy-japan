"""Private Home Assistant Store backend for per-supply-point sync checkpoints."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers.storage import Store

from .background_sync import CHECKPOINT_SCHEMA_VERSION, SyncCheckpointBackend
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_ENTRY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_STORAGE_SCOPE_PATTERN = re.compile(r"supply-point-[0-9a-f]{64}")


class HomeAssistantSyncCheckpointBackend(SyncCheckpointBackend):
    """Atomically persist one private versioned synchronization checkpoint."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        storage_scope: str,
    ) -> None:
        if _ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
            raise ValueError("Sync checkpoint entry_id contains unsafe characters")
        if _STORAGE_SCOPE_PATTERN.fullmatch(storage_scope) is None:
            raise ValueError("Sync checkpoint scope must be an opaque supply-point identity")
        self._store = Store[dict[str, Any]](
            hass,
            CHECKPOINT_SCHEMA_VERSION,
            f"{DOMAIN}.sync.{entry_id}.{storage_scope}",
            private=True,
            atomic_writes=True,
            serialize_in_event_loop=False,
        )

    async def async_load(self) -> Mapping[str, Any] | None:
        """Load one checkpoint payload."""
        return cast("Mapping[str, Any] | None", await self._store.async_load())

    async def async_save(self, payload: Mapping[str, Any]) -> None:
        """Persist immediately after ledger durability."""
        await self._store.async_save(deepcopy(dict(payload)))
