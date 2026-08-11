"""Typed runtime discovery state and privacy-preserving HA device projection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
from .const import CONF_CONNECTION_LABEL, CONF_ENABLED_HISTORICAL_RESOURCES, DOMAIN
from .identity import stable_account_identity, stable_supply_point_identity

if TYPE_CHECKING:
    from .commercial_coordinator import OejpCommercialCoordinator
    from .coordinator import OejpDataUpdateCoordinator
    from .tariff_history_store import TariffHistoryArchive


@dataclass(slots=True)
class OejpRuntimeData:
    """Runtime data owned by one OAuth login-scoped config entry."""

    auth: AuthSession
    accounts: tuple[OejpAccount, ...]
    capabilities: CapabilitySnapshot
    identity_secret: str
    coordinator: OejpDataUpdateCoordinator | None = None
    commercial_coordinator: OejpCommercialCoordinator | None = None
    # The archive of rate adjustments the provider stops serving. Held here so diagnostics and
    # the repair issues can report what it holds and whether any of it failed to load.
    tariff_archive: TariffHistoryArchive | None = None

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
                continue
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


def connection_label(entry: ConfigEntry) -> str:
    """Return the name the user gave this login, or an empty string.

    Device names are ordinals within one entry, which is unambiguous until somebody adds a
    second login: both entries then produce `OEJP account 1` and `OEJP supply point 1-1`.
    Measured on an installation with two logins — one current address, one from before a
    move — the names were identical, and the only way to tell them apart was to rename every
    device by hand.

    An ordinal cannot be made unique across entries without becoming unstable: it would have
    to depend on how many other entries exist, and removing one would renumber the rest. A
    name the user chooses is stable, means something to them, and stays out of entity ids.
    """
    label = entry.options.get(CONF_CONNECTION_LABEL)
    return label.strip() if isinstance(label, str) else ""


def selected_historical_resources(entry: ConfigEntry) -> frozenset[str]:
    """Return validated installation-local resource identities from options."""
    selected = entry.options.get(CONF_ENABLED_HISTORICAL_RESOURCES, ())
    if not isinstance(selected, list | tuple):
        return frozenset()
    return frozenset(value for value in selected if isinstance(value, str) and value)


def normalize_historical_selection(
    accounts: Iterable[OejpAccount],
    identity_secret: str,
    requested: Iterable[str],
) -> tuple[str, ...]:
    """Validate history selection and reject stale or redundant child choices."""
    requested_set = frozenset(requested)
    enabled: set[str] = set()
    for account in accounts:
        account_identity = stable_account_identity(identity_secret, account.number)
        if account.lifecycle is ResourceLifecycle.HISTORICAL:
            if account_identity in requested_set:
                enabled.add(account_identity)
            # A historical account owns selection of every child. Child-only
            # choices under an unselected account are stale and ignored.
            continue
        for supply_point in _iter_supply_points(account):
            if supply_point.lifecycle is not ResourceLifecycle.HISTORICAL:
                continue
            supply_point_identity = stable_supply_point_identity(
                identity_secret,
                account.number,
                supply_point.id,
            )
            if supply_point_identity in requested_set:
                enabled.add(supply_point_identity)
    return tuple(sorted(enabled))


def async_project_discovered_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: OejpRuntimeData,
) -> None:
    """Create/update account and supply-point devices without exposing provider IDs."""
    registry = dr.async_get(hass)
    selected = selected_historical_resources(entry)
    label = connection_label(entry)
    prefix = f"OEJP {label}" if label else "OEJP"
    integration_disabler = dr.DeviceEntryDisabler.INTEGRATION
    discovered_identities: set[str] = set()
    for account_number, account in enumerate(runtime.accounts, start=1):
        account_identity = stable_account_identity(runtime.identity_secret, account.number)
        discovered_identities.add(account_identity)
        account_selected = account_identity in selected
        account_disabled = (
            account.lifecycle is ResourceLifecycle.HISTORICAL and not account_selected
        )
        account_identifiers = {(DOMAIN, account_identity)}
        existing_account = registry.async_get_device(identifiers=account_identifiers)
        account_device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            disabled_by=(
                existing_account.disabled_by
                if existing_account is not None
                else (integration_disabler if account_disabled else None)
            ),
            identifiers=account_identifiers,
            manufacturer="Octopus Energy Japan",
            model="Electricity account",
            name=f"{prefix} account {account_number}",
            # The one place a provider identifier is shown, on purpose. Names and
            # entity ids stay ordinal so they can be screenshotted and pasted into an
            # issue, but a customer with more than one account still has to be able to
            # tell which is which, and Home Assistant's device page is where a device's
            # own serial belongs. It never enters an entity id, a state, an attribute,
            # or the diagnostics download.
            serial_number=account.number,
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
            discovered_identities.add(supply_point_identity)
            supply_point_disabled = account_disabled or (
                supply_point.lifecycle is ResourceLifecycle.HISTORICAL
                and not account_selected
                and supply_point_identity not in selected
            )
            supply_point_identifiers = {(DOMAIN, supply_point_identity)}
            existing_supply_point = registry.async_get_device(identifiers=supply_point_identifiers)
            supply_point_device = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                disabled_by=(
                    existing_supply_point.disabled_by
                    if existing_supply_point is not None
                    else (integration_disabler if supply_point_disabled else None)
                ),
                identifiers=supply_point_identifiers,
                manufacturer="Octopus Energy Japan",
                model="Electricity supply point",
                name=f"{prefix} supply point {account_number}-{supply_point_number}",
                # The supply-point number (供給地点特定番号) as OEJP prints it on a
                # bill, so a household with two points can match a device to a
                # contract. `spin` is the customer-facing one; the internal id is the
                # fallback when the provider omits it.
                serial_number=supply_point.spin or supply_point.id,
                via_device=(DOMAIN, account_identity),
            )
            _sync_device_disabled(
                registry,
                supply_point_device,
                disabled=supply_point_disabled,
                integration_disabler=integration_disabler,
            )

    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        identities = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
        if identities and identities.isdisjoint(discovered_identities):
            _sync_device_disabled(
                registry,
                device,
                disabled=True,
                integration_disabler=integration_disabler,
            )


def _iter_supply_points(account: OejpAccount) -> tuple[OejpSupplyPoint, ...]:
    return tuple(
        supply_point for property_ in account.properties for supply_point in property_.supply_points
    )


def _sync_device_disabled(
    registry: dr.DeviceRegistry,
    device: dr.DeviceEntry,
    *,
    disabled: bool,
    integration_disabler: dr.DeviceEntryDisabler,
) -> None:
    """Only change integration-owned disabling; preserve a user's choice."""
    if disabled and device.disabled_by is None:
        registry.async_update_device(device.id, disabled_by=integration_disabler)
    elif not disabled and device.disabled_by == integration_disabler:
        registry.async_update_device(device.id, disabled_by=None)
