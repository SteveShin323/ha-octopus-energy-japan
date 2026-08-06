"""Low-cadence optional account, agreement, product, and billing coordinator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    AccountCommercialSnapshot,
    AuthenticatedGraphQLClient,
    CommercialAccess,
    CommercialAvailability,
    CommercialFeature,
    OejpAccount,
    OejpAuthenticationError,
    OejpError,
    SupplyPointTariff,
    async_fetch_account_commercial_snapshot,
    async_fetch_supply_point_tariffs,
)
from .const import DOMAIN
from .tariff_history_store import TariffHistoryArchive

COMMERCIAL_UPDATE_INTERVAL = timedelta(hours=12)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OejpCommercialData:
    """Immutable optional commercial snapshot for every discovered account."""

    accounts: tuple[AccountCommercialSnapshot, ...]
    observed_at: datetime
    # Keyed by (account number, supply point id). The tariff is what turns consumption
    # into a cost the Energy dashboard can show, and it lives on the agreement rather
    # than the account, so it is collected here beside the other optional reads.
    tariffs: tuple[SupplyPointTariff, ...] = ()

    def account(self, account_id: str) -> AccountCommercialSnapshot | None:
        """Return a commercial snapshot by raw runtime-only account ID."""
        return next((value for value in self.accounts if value.account_id == account_id), None)

    def tariff(self, account_id: str, supply_point_id: str) -> SupplyPointTariff | None:
        """Return the tariff for one supply point, if the provider reported one."""
        return next(
            (
                value
                for value in self.tariffs
                if value.account_number == account_id and value.supply_point_id == supply_point_id
            ),
            None,
        )


class OejpCommercialCoordinator(DataUpdateCoordinator[OejpCommercialData]):
    """Refresh optional commercial data without affecting consumption."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AuthenticatedGraphQLClient,
        accounts: tuple[OejpAccount, ...],
        *,
        now: Callable[[], datetime] | None = None,
        archive: TariffHistoryArchive | None = None,
    ) -> None:
        current = (now or (lambda: datetime.now(UTC)))()
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_commercial",
            update_interval=COMMERCIAL_UPDATE_INTERVAL,
            config_entry=entry,
            always_update=False,
        )
        self._client = client
        self._accounts = accounts
        self._now = now or (lambda: datetime.now(UTC))
        # The adjustments the provider stops serving once their period ends. Filed here because
        # this is the only place a tariff is read, and a value missed is a value gone for good.
        self._archive = archive
        self.data = OejpCommercialData((), _utc(current))

    def set_accounts(self, accounts: tuple[OejpAccount, ...]) -> None:
        """Update the discovery roster used by the next commercial refresh."""
        self._accounts = accounts

    async def _async_update_data(self) -> OejpCommercialData:
        observed_at = _utc(self._now())
        previous = {snapshot.account_id: snapshot for snapshot in self.data.accounts}
        previous_tariffs = self.data.tariffs
        snapshots: list[AccountCommercialSnapshot] = []
        for account in self._accounts:
            try:
                snapshot = await async_fetch_account_commercial_snapshot(
                    self._client,
                    account.number,
                    observed_at=observed_at,
                )
            except OejpAuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    "OEJP OAuth authorization must be renewed",
                    translation_domain=DOMAIN,
                    translation_key="reauth_required",
                ) from err
            except OejpError:
                snapshot = _failed_snapshot(
                    account.number, previous.get(account.number), observed_at
                )
            snapshots.append(snapshot)

        tariffs: list[SupplyPointTariff] = []
        for account in self._accounts:
            try:
                tariffs.extend(await async_fetch_supply_point_tariffs(self._client, account.number))
            except OejpAuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    "OEJP OAuth authorization must be renewed",
                    translation_domain=DOMAIN,
                    translation_key="reauth_required",
                ) from err
            except OejpError:
                # A tariff this account cannot read costs only the cost series. Keeping
                # whatever was read before is better than dropping a working price.
                tariffs.extend(
                    value for value in previous_tariffs if value.account_number == account.number
                )
        if self._archive is not None:
            await self._archive.async_observe(tariffs, observed_at=observed_at)
        return OejpCommercialData(tuple(snapshots), observed_at, tuple(tariffs))


def _failed_snapshot(
    account_id: str,
    previous: AccountCommercialSnapshot | None,
    observed_at: datetime,
) -> AccountCommercialSnapshot:
    """Preserve last safe data while marking every optional operation failed."""
    access = tuple(
        CommercialAccess(feature, CommercialAvailability.FAILED) for feature in CommercialFeature
    )
    if previous is None:
        return AccountCommercialSnapshot(account_id, access=access, observed_at=observed_at)
    return AccountCommercialSnapshot(
        account_id,
        previous.overview,
        previous.agreements,
        previous.latest_bill,
        previous.latest_transaction,
        access,
        observed_at,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Commercial coordinator timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "COMMERCIAL_UPDATE_INTERVAL",
    "OejpCommercialCoordinator",
    "OejpCommercialData",
]
