"""Typed runtime discovery state and privacy-preserving HA device projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .api import (
    AuthSession,
    CapabilitySnapshot,
    OejpAccount,
    OejpSupplyPoint,
    ResourceLifecycle,
)
from .const import CONF_ENABLED_HISTORICAL_RESOURCES, DOMAIN
from .identity import stable_account_identity, stable_supply_point_identity

if TYPE_CHECKING:
    from .coordinator import OejpDataUpdateCoordinator


@dataclass(slots=True)
class OejpRuntimeData:
    """Runtime data owned by one OAuth login-scoped config entry."""

    auth: AuthSession
    accounts: tuple[OejpAccount, ...]
    capabilities: CapabilitySnapshot
    identity_secret: str
    coordinator: OejpDataUpdateCoordinator | None = None

    def historical_resource_options(self) -> dict[str, str]:
        """Return safe labels keyed by installation-local resource identities."""
        options: dict[str, str] = {}
        historical_account_number = 0
        historical_supply_point_number = 0
        for account in self.accounts:
            if account.lifecycle is ResourceLifecycle.HISTORICAL:
                historical_account_number += 1
                options[stable_account_identity(self.identity_secret, account.number)] = (
                    f"Historical account {historical_account_number}"
                )
            for supply_point in _iter_supply_points(account):
                if supply_point.lifecycle is ResourceLifecycle.HISTORICAL:
                    historical_supply_point_number += 1
                    options[
                        stable_supply_point_identity(
                            self.identity_secret,
                            account.number,
                            supply_point.id,
                        )
                    ] = f"Historical supply point {historical_supply_point_number}"
        return options


def selected_historical_resources(entry: ConfigEntry) -> frozenset[str]:
    """Return validated installation-local resource identities from options."""
    selected = entry.options.get(CONF_ENABLED_HISTORICAL_RESOURCES, ())
    if not isinstance(selected, list | tuple):
        return frozenset()
    return frozenset(value for value in selected if isinstance(value, str) and value)


def async_project_discovered_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: OejpRuntimeData,
) -> None:
    """Create/update account and supply-point devices without exposing provider IDs."""
    registry = dr.async_get(hass)
    selected = selected_historical_resources(entry)
    integration_disabler = _integration_device_disabler()
    for account_number, account in enumerate(runtime.accounts, start=1):
        account_identity = stable_account_identity(runtime.identity_secret, account.number)
        account_disabled = (
            account.lifecycle is ResourceLifecycle.HISTORICAL and account_identity not in selected
        )
        account_device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            disabled_by=(integration_disabler if account_disabled else None),
            identifiers={(DOMAIN, account_identity)},
            manufacturer="Octopus Energy Japan",
            model="Electricity account",
            name=f"OEJP account {account_number}",
        )
        _sync_device_disabled(
            registry,
            account_device,
            disabled=account_disabled,
            integration_disabler=integration_disabler,
        )

        for supply_point_number, supply_point in enumerate(
            _iter_supply_points(account),
            start=1,
        ):
            supply_point_identity = stable_supply_point_identity(
                runtime.identity_secret,
                account.number,
                supply_point.id,
            )
            supply_point_disabled = account_disabled or (
                supply_point.lifecycle is ResourceLifecycle.HISTORICAL
                and supply_point_identity not in selected
            )
            supply_point_device = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                disabled_by=(integration_disabler if supply_point_disabled else None),
                identifiers={(DOMAIN, supply_point_identity)},
                manufacturer="Octopus Energy Japan",
                model="Electricity supply point",
                name=f"OEJP supply point {account_number}-{supply_point_number}",
                via_device=(DOMAIN, account_identity),
            )
            _sync_device_disabled(
                registry,
                supply_point_device,
                disabled=supply_point_disabled,
                integration_disabler=integration_disabler,
            )


def _iter_supply_points(account: OejpAccount) -> tuple[OejpSupplyPoint, ...]:
    return tuple(
        supply_point for property_ in account.properties for supply_point in property_.supply_points
    )


def _integration_device_disabler() -> Any:
    """Bridge the Home Assistant 2026.8 device disabler enum rename."""
    disabler_type = getattr(dr, "DeviceEntryDisabler", None)
    if disabler_type is None:
        disabler_type = dr.__dict__["RegistryEntryDisabler"]
    return disabler_type.INTEGRATION


def _sync_device_disabled(
    registry: Any,
    device: Any,
    *,
    disabled: bool,
    integration_disabler: Any,
) -> None:
    """Only change integration-owned disabling; preserve a user's choice."""
    if disabled and device.disabled_by is None:
        registry.async_update_device(device.id, disabled_by=integration_disabler)
    elif not disabled and device.disabled_by == integration_disabler:
        registry.async_update_device(device.id, disabled_by=None)
