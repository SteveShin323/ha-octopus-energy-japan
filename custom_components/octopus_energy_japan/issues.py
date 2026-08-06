"""Repair issues raised from observable runtime state.

Every issue here is informational rather than fixable in place. The conditions are
provider-side or storage-side, so there is no in-app action that would resolve
them, and offering a repair flow that cannot repair anything would be misleading.
Each issue instead explains the effect and links to the documentation that says
what to do next.

Issue identities are scoped to the config entry and, where relevant, to an
installation-local HMAC identity. No raw provider identifier is used, because
issue identities appear in the Home Assistant UI and in its storage.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import Capability, CapabilityAvailability, CommercialAvailability
from .const import DOMAIN

if TYPE_CHECKING:
    from datetime import datetime

    from .commercial_coordinator import OejpCommercialData
    from .coordinator import OejpCoordinatorData
    from .tariff_history_store import TariffHistoryArchive

# Each issue already carries its own translated title and description, so this link
# only has to land somewhere useful. Troubleshooting is that place.
DOCS_BASE: Final = (
    "https://github.com/SteveShin323/ha-octopus-energy-japan/blob/main/README.md#troubleshooting"
)

# A direction that has not produced a reading for this long is reported. OEJP
# publishes half-hourly readings with normal delays of several hours, and the
# regular poll already overlaps 72 hours, so a shorter threshold would report
# ordinary provider lag as a fault.
READING_SILENCE_THRESHOLD: Final = timedelta(hours=36)


class OejpIssue(StrEnum):
    """Stable repair-issue kinds."""

    LEDGER_PARTITIONS_CORRUPT = "ledger_partitions_corrupt"
    READINGS_SILENT = "readings_silent"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    COMMERCIAL_PERMISSION_MISSING = "commercial_permission_missing"
    TARIFF_NOT_PRICEABLE = "tariff_not_priceable"
    TARIFF_HISTORY_UNREADABLE = "tariff_history_unreadable"


def _issue_id(entry_id: str, issue: OejpIssue) -> str:
    return f"{entry_id}_{issue.value}"


def _apply(
    hass: HomeAssistant,
    entry_id: str,
    issue: OejpIssue,
    *,
    active: bool,
    placeholders: dict[str, str] | None = None,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
) -> None:
    issue_id = _issue_id(entry_id, issue)
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=severity,
        translation_key=issue.value,
        translation_placeholders=placeholders,
        learn_more_url=DOCS_BASE,
    )


def _silent_directions(data: OejpCoordinatorData, now: datetime) -> int:
    silent = 0
    for status in data.direction_statuses:
        if not status.queryable:
            continue
        if (
            status.last_success_at is None
            or now - status.last_success_at > READING_SILENCE_THRESHOLD
        ):
            silent += 1
    return silent


def _unavailable_capabilities(data: OejpCoordinatorData) -> tuple[str, ...]:
    lost = {
        CapabilityAvailability.UNSUPPORTED,
        CapabilityAvailability.FORBIDDEN,
    }
    return tuple(
        sorted(
            status.capability.value
            for status in data.capabilities.statuses
            if status.availability in lost
            # Reading capabilities matter to the user. Optional topology gaps are
            # normal for a supply point without devices and are not a fault.
            and status.capability
            in {
                Capability.GENERIC_READINGS,
                Capability.LEGACY_HALF_HOURLY_READINGS,
                Capability.LEGACY_INTERVAL_READINGS,
            }
        )
    )


def _forbidden_commercial_features(data: OejpCommercialData | None) -> tuple[str, ...]:
    if data is None:
        return ()
    forbidden: set[str] = set()
    for account in data.accounts:
        for status in account.access:
            if status.availability is CommercialAvailability.FORBIDDEN:
                forbidden.add(status.feature.value)
    return tuple(sorted(forbidden))


def _unpriceable_tariff_reasons(commercial: OejpCommercialData | None) -> tuple[str, ...]:
    """Return the distinct reasons a reported tariff cannot be priced.

    A plan shape this formula cannot express is refused rather than approximated, which leaves
    the cost statistic absent. Without this the absence is indistinguishable from a defect, and
    the user has no way to learn that their plan, not the integration, is the reason.

    A supply point with no consumption agreement at all yields no tariff and so no reason: an
    export-only point is not a problem to report.
    """
    if commercial is None:
        return ()
    return tuple(
        sorted(
            {
                tariff.unpriceable_reason.value
                for tariff in commercial.tariffs
                if tariff.unpriceable_reason is not None
            }
        )
    )


@callback
def async_update_issues(
    hass: HomeAssistant,
    entry_id: str,
    data: OejpCoordinatorData,
    commercial: OejpCommercialData | None,
    now: datetime,
    archive: TariffHistoryArchive | None = None,
) -> None:
    """Raise or clear every issue this runtime state implies."""
    _apply(
        hass,
        entry_id,
        OejpIssue.LEDGER_PARTITIONS_CORRUPT,
        active=data.corrupt_partition_count > 0,
        placeholders={"count": str(data.corrupt_partition_count)},
        severity=ir.IssueSeverity.ERROR,
    )

    silent = _silent_directions(data, now)
    _apply(
        hass,
        entry_id,
        OejpIssue.READINGS_SILENT,
        active=silent > 0,
        placeholders={
            "count": str(silent),
            "hours": str(int(READING_SILENCE_THRESHOLD.total_seconds() // 3600)),
        },
    )

    unavailable = _unavailable_capabilities(data)
    _apply(
        hass,
        entry_id,
        OejpIssue.CAPABILITY_UNAVAILABLE,
        active=bool(unavailable),
        placeholders={"capabilities": ", ".join(unavailable)},
    )

    forbidden = _forbidden_commercial_features(commercial)
    _apply(
        hass,
        entry_id,
        OejpIssue.COMMERCIAL_PERMISSION_MISSING,
        active=bool(forbidden),
        placeholders={"features": ", ".join(forbidden)},
        severity=ir.IssueSeverity.WARNING,
    )

    unpriceable = _unpriceable_tariff_reasons(commercial)
    _apply(
        hass,
        entry_id,
        OejpIssue.TARIFF_NOT_PRICEABLE,
        active=bool(unpriceable),
        placeholders={"reasons": ", ".join(unpriceable)},
        severity=ir.IssueSeverity.WARNING,
    )

    quarantined = archive.quarantined_supply_points if archive is not None else 0
    _apply(
        hass,
        entry_id,
        OejpIssue.TARIFF_HISTORY_UNREADABLE,
        active=quarantined > 0,
        placeholders={"count": str(quarantined)},
        severity=ir.IssueSeverity.WARNING,
    )


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove every issue owned by one config entry."""
    for issue in OejpIssue:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id, issue))
