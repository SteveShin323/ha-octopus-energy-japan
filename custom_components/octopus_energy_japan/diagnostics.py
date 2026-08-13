"""Privacy-preserving config-entry diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .api import CapabilitySnapshot, ResourceLifecycle
from .const import DOMAIN
from .identity import stable_supply_point_identity
from .ledger import LEDGER_SCHEMA_VERSION
from .tariff_history import TARIFF_HISTORY_SCHEMA_VERSION

if TYPE_CHECKING:
    from .commercial_coordinator import OejpCommercialCoordinator
    from .coordinator import OejpCoordinatorData, OejpDataUpdateCoordinator

# Every value below is either a constant, a count, a boolean, an enumerated state,
# an installation-local HMAC identity, or a UTC timestamp. Raw account numbers,
# SPINs, supply-point and meter identifiers, addresses, names, email addresses,
# tokens, reading values, provider cost, bill amounts, and provider message text
# are never included. `docs/ARCHITECTURE.md` is the controlling contract.


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _capabilities(capabilities: CapabilitySnapshot) -> list[dict[str, Any]]:
    return [
        {
            "capability": status.capability.value,
            "availability": status.availability.value,
            "reason": status.reason,
        }
        for status in capabilities.statuses
    ]


def _resources(data: OejpCoordinatorData) -> dict[str, Any]:
    supply_points = 0
    historical_supply_points = 0
    historical_accounts = 0
    for account in data.accounts:
        if account.lifecycle is ResourceLifecycle.HISTORICAL:
            historical_accounts += 1
        for property_ in account.properties:
            for supply_point in property_.supply_points:
                supply_points += 1
                if supply_point.lifecycle is ResourceLifecycle.HISTORICAL:
                    historical_supply_points += 1
    return {
        "accounts": len(data.accounts),
        "historical_accounts": historical_accounts,
        "supply_points": supply_points,
        "historical_supply_points": historical_supply_points,
        "present_supply_points": len(data.present_supply_points),
        "enabled_supply_points": len(data.enabled_supply_points),
    }


def _directions(data: OejpCoordinatorData) -> list[dict[str, Any]]:
    return [
        {
            "account": status.account_identity,
            "supply_point": status.supply_point_identity,
            "direction": status.direction.value,
            "queryable": status.queryable,
            "stale": status.stale,
            "last_success_at": _timestamp(status.last_success_at),
            "error_class": status.error_class.value if status.error_class else None,
            "coverage_start_at": _timestamp(status.coverage_start_at),
            "coverage_end_at": _timestamp(status.coverage_end_at),
            "background_coverage_windows": len(status.background_coverage),
            "backfill_state": status.backfill_state.value if status.backfill_state else None,
            "backfill_cursor": _timestamp(status.backfill_cursor),
            "backfill_empty_streak": status.backfill_empty_streak,
        }
        for status in data.direction_statuses
    ]


def _providers(data: OejpCoordinatorData) -> list[dict[str, Any]]:
    return [
        {
            "account": observation.account_identity,
            "supply_point": observation.supply_point_identity,
            "direction": observation.direction.value,
            "provider": observation.provider.value,
            "fallback_reason": (
                observation.fallback_reason.value if observation.fallback_reason else None
            ),
            "observed_at": _timestamp(observation.observed_at),
        }
        for observation in data.provider_observations
    ]


def _aggregation(data: OejpCoordinatorData) -> dict[str, Any]:
    intervals = sum(
        projection.this_month.interval_count + projection.last_month.interval_count
        for projection in data.aggregation.supply_points
    )
    delays = [
        projection.data_delay.total_seconds()
        for projection in data.aggregation.supply_points
        if projection.data_delay is not None
    ]
    return {
        "generated_at": _timestamp(data.aggregation.generated_at),
        "timezone": data.aggregation.timezone,
        "projections": len(data.aggregation.supply_points),
        "recent_intervals": intervals,
        "max_data_delay_seconds": max(delays) if delays else None,
    }


def _tariffs(
    coordinator: OejpCommercialCoordinator | None,
    identity_secret: str,
) -> list[dict[str, Any]]:
    """Report the shape of each reported tariff, never a price.

    A cost statistic that is absent looks the same whether the plan cannot be expressed or the
    integration is broken. What the provider said the plan is — its product type, how many
    steps and rate generations it has, and what the standing charge is measured in — is what
    distinguishes the two, and none of it is a monetary amount or an identifier.
    """
    snapshot = coordinator.data if coordinator is not None else None
    if snapshot is None:
        return []
    return [
        {
            "supply_point": stable_supply_point_identity(
                identity_secret,
                tariff.account_number,
                tariff.supply_point_id,
            ),
            "product_type": tariff.product_type,
            "priceable": tariff.is_priceable,
            "unpriceable_reason": (
                tariff.unpriceable_reason.value if tariff.unpriceable_reason else None
            ),
            "steps": len(tariff.steps),
            "rate_generations": len({(step.valid_from, step.valid_to) for step in tariff.steps}),
            # A time-of-use tariff is priced by the hour instead, so the scheme it follows and
            # the slots the provider named are what a wrong cost has to be traced through. The
            # slots are the provider's own labels with the grid area stripped, not amounts.
            "time_of_use_scheme": tariff.tou_scheme,
            "grid_operator_code": tariff.grid_operator_code,
            "time_of_use_slots": sorted({band.slot for band in tariff.bands}),
            "standing_charge_unit": tariff.standing_charge_unit,
            "has_standing_charge": tariff.standing_charge_per_day is not None,
            "has_fuel_cost_adjustment": tariff.fuel_cost_adjustment is not None,
            "has_renewable_energy_levy": tariff.renewable_energy_levy is not None,
            # True when this is priced from an agreement that has already ended. The adders it
            # uses may be extrapolated more than a live tariff's — see
            # `extrapolated_adder_hours` below — which is why this is called out on its own.
            "is_estimate": tariff.is_estimate,
        }
        for tariff in snapshot.tariffs
    ]


def _billing_periods(data: OejpCoordinatorData, identity_secret: str) -> list[dict[str, Any]]:
    """Report which day each supply point's charges restart on, and what said so.

    The rule that a period runs from one meter reading to the day before the next was measured
    on one account with one closed invoice. A user whose bill does not line up needs to be able
    to say which evidence produced their anchor, so the source is reported alongside it.
    """
    from .coordinator import billing_periods_for, iter_supply_points

    return [
        {
            "supply_point": stable_supply_point_identity(
                identity_secret,
                account.number,
                point.id,
            ),
            "anchor_day": calendar.anchor_day,
            "source": calendar.source.value,
            "reading_day_of_month": point.reading_day_of_month,
            "agrees_with_reported_reading_day": (
                None
                if calendar.anchor_day is None or point.reading_day_of_month is None
                else calendar.anchor_day == point.reading_day_of_month
            ),
        }
        for account in data.accounts
        for point in iter_supply_points(account)
        if (calendar := billing_periods_for(point))
    ]


def _commercial(coordinator: OejpCommercialCoordinator | None) -> dict[str, Any]:
    if coordinator is None:
        return {"configured": False}
    snapshot = coordinator.data
    return {
        "configured": True,
        "last_update_success": coordinator.last_update_success,
        "observed_at": _timestamp(snapshot.observed_at if snapshot else None),
        "accounts": [
            {
                "features": [
                    {
                        "feature": status.feature.value,
                        "availability": status.availability.value,
                        "error_codes": list(status.error_codes),
                        "error_types": list(status.error_types),
                        "error_paths": [list(path) for path in status.error_paths],
                    }
                    for status in account.access
                ],
                "has_overview": account.overview is not None,
                "agreements": len(account.agreements),
                "has_latest_bill": account.latest_bill is not None,
                "has_latest_transaction": account.latest_transaction is not None,
            }
            for account in (snapshot.accounts if snapshot else ())
        ],
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return a redacted snapshot suitable for a public issue report."""
    from .runtime import OejpRuntimeData

    integration = await async_get_integration(hass, DOMAIN)
    report: dict[str, Any] = {
        "integration": {
            "domain": DOMAIN,
            "version": integration.version and str(integration.version),
            "home_assistant": HA_VERSION,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
        },
        "config_entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "source": entry.source,
            "has_stored_token": bool(entry.data.get("token")),
            "has_auth_implementation": bool(entry.data.get("auth_implementation")),
            "selected_historical_resources": len(
                entry.options.get("enabled_historical_resources", []) or []
            ),
        },
    }

    runtime = entry.runtime_data
    if not isinstance(runtime, OejpRuntimeData):
        report["runtime"] = {"loaded": False}
        return report

    coordinator: OejpDataUpdateCoordinator | None = runtime.coordinator
    if coordinator is None:
        report["runtime"] = {"loaded": False, "capabilities": _capabilities(runtime.capabilities)}
        return report

    data = coordinator.data
    report["runtime"] = {
        "loaded": True,
        "last_update_success": coordinator.last_update_success,
        "last_exception": type(coordinator.last_exception).__name__
        if coordinator.last_exception
        else None,
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        ),
        "capabilities": _capabilities(data.capabilities),
        "resources": _resources(data),
        "corrections": data.correction_count,
        "last_refresh_changes": data.last_refresh_change_count,
        "corrupt_partitions": data.corrupt_partition_count,
        "discarded_checkpoints": data.discarded_checkpoint_count,
    }
    report["directions"] = _directions(data)
    report["providers"] = _providers(data)
    report["aggregation"] = _aggregation(data)
    report["commercial"] = _commercial(runtime.commercial_coordinator)
    report["tariffs"] = _tariffs(runtime.commercial_coordinator, runtime.identity_secret)
    report["billing_periods"] = _billing_periods(data, runtime.identity_secret)
    # Holes in the collected history. Reading every stored month to find them is why this is
    # here rather than on the poll: a download is asked for once, a refresh happens twice an hour.
    report["history_gaps"] = [
        {
            "account": gap.account,
            "supply_point": gap.supply_point,
            "direction": gap.direction.value,
            "gaps": gap.gaps,
            "missing_hours": gap.missing_hours,
            "earliest_gap_at": _timestamp(gap.earliest_gap_at),
            "latest_gap_end_at": _timestamp(gap.latest_gap_end_at),
        }
        for gap in await coordinator.async_history_gaps()
    ]
    # Windows the provider has been taken at its word about. A permanently short history needs an
    # explanation, and this is it.
    report["abandoned_gap_windows"] = coordinator.abandoned_gap_windows()
    # How many of each supply point's priced hours needed an adder extrapolated from the
    # nearest archived value rather than one that actually covered them. What makes an
    # `is_estimate` tariff's cost an estimate rather than a bill, made visible instead of assumed.
    report["extrapolated_adder_hours"] = coordinator.extrapolated_adder_hours()
    archive = runtime.tariff_archive
    report["tariff_history"] = {
        "schema_version": TARIFF_HISTORY_SCHEMA_VERSION,
        "archived_records": archive.archived_records if archive else 0,
        "revised_windows": archive.revised_windows if archive else 0,
        "quarantined_supply_points": archive.quarantined_supply_points if archive else 0,
    }
    return report
