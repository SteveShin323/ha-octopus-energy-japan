#!/usr/bin/env python3
"""Run allow-listed OEJP read operations and emit only synthetic fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

if __package__ is None and __name__ == "__main__":  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import ClientSession
from custom_components.octopus_energy_japan.api import (
    ACCOUNT_AGREEMENTS_QUERY,
    ACCOUNT_BILLING_QUERY,
    ACCOUNT_OVERVIEW_QUERY,
    CAPABILITY_QUERY,
    ELECTRICITY_MARKET_NAME,
    GENERIC_DEVICES_QUERY,
    LEGACY_DISCOVERY_QUERY,
    LEGACY_HALF_HOURLY_QUERY,
    LEGACY_INTERVAL_QUERY,
    OejpGraphQLClient,
    ReadingDirection,
)
from custom_components.octopus_energy_japan.api.operations import (
    VIEWER_ACCOUNTS_QUERY,
    VIEWER_IDENTITY_QUERY,
    async_obtain_token,
)
from custom_components.octopus_energy_japan.api.readings import (
    GENERIC_ENERGY_UNITS,
    GENERIC_PAGE_SIZE,
    build_generic_readings_query,
)
from custom_components.octopus_energy_japan.probe import build_contract_fixture

DEFAULT_WINDOW_HOURS: Final = 48
ACCOUNT_NUMBER_ENV: Final = "OEJP_PROBE_ACCOUNT_NUMBER"
SUPPLY_POINT_ENV: Final = "OEJP_PROBE_SUPPLY_POINT_SPIN"


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Local-only inputs for a parameterized read-only probe."""

    account_number: str | None
    supply_point_spin: str | None
    start_at: datetime
    end_at: datetime

    def account(self) -> str:
        """Return the resolved account number or explain how to choose one."""
        if not self.account_number:
            raise RuntimeError(f"No account was discovered; set {ACCOUNT_NUMBER_ENV} to choose one")
        return self.account_number

    def supply_point(self) -> str:
        """Return the resolved supply-point SPIN or explain how to choose one."""
        if not self.supply_point_spin:
            raise RuntimeError(
                f"No supply point was discovered; set {SUPPLY_POINT_ENV} to choose one"
            )
        return self.supply_point_spin

    def graphql_start(self) -> str:
        """Return the window start in the provider's expected format."""
        return _graphql_datetime(self.start_at)

    def graphql_end(self) -> str:
        """Return the window end in the provider's expected format."""
        return _graphql_datetime(self.end_at)


@dataclass(frozen=True, slots=True)
class ReadOnlyOperation:
    """One fixed read-only probe operation and its bounded variables."""

    name: str
    query: str
    variables: Callable[[ProbeContext], dict[str, Any]] | None = None


def _account_variables(context: ProbeContext) -> dict[str, Any]:
    return {"accountNumber": context.account()}


def _agreement_variables(context: ProbeContext) -> dict[str, Any]:
    return {"accountNumber": context.account(), "after": None}


def _half_hourly_variables(context: ProbeContext) -> dict[str, Any]:
    return {
        "accountNumber": context.account(),
        "fromDatetime": context.graphql_start(),
        "toDatetime": context.graphql_end(),
    }


def _interval_variables(context: ProbeContext) -> dict[str, Any]:
    return {
        "accountNumber": context.account(),
        "startAt": context.graphql_start(),
        "endAt": context.graphql_end(),
    }


def _supply_point_variables(context: ProbeContext) -> dict[str, Any]:
    return {
        "externalIdentifier": context.supply_point(),
        "marketName": ELECTRICITY_MARKET_NAME,
    }


def _generic_reading_variables(context: ProbeContext) -> dict[str, Any]:
    return {
        **_supply_point_variables(context),
        "startAt": context.graphql_start(),
        "endAt": context.graphql_end(),
        "units": list(GENERIC_ENERGY_UNITS),
        "first": GENERIC_PAGE_SIZE,
        "after": None,
    }


OPERATIONS: Final = {
    operation.name: operation
    for operation in (
        ReadOnlyOperation("viewer_identity", VIEWER_IDENTITY_QUERY),
        ReadOnlyOperation("viewer_accounts", VIEWER_ACCOUNTS_QUERY),
        ReadOnlyOperation("resource_discovery", LEGACY_DISCOVERY_QUERY),
        ReadOnlyOperation("schema_capabilities", CAPABILITY_QUERY),
        ReadOnlyOperation(
            "generic_devices",
            GENERIC_DEVICES_QUERY,
            _supply_point_variables,
        ),
        ReadOnlyOperation(
            "generic_import_readings",
            build_generic_readings_query("supply_point", ReadingDirection.IMPORT, True),
            _generic_reading_variables,
        ),
        ReadOnlyOperation(
            "generic_export_readings",
            build_generic_readings_query("supply_point", ReadingDirection.EXPORT, True),
            _generic_reading_variables,
        ),
        ReadOnlyOperation(
            "legacy_half_hourly_readings",
            LEGACY_HALF_HOURLY_QUERY,
            _half_hourly_variables,
        ),
        ReadOnlyOperation(
            "legacy_interval_readings",
            LEGACY_INTERVAL_QUERY,
            _interval_variables,
        ),
        ReadOnlyOperation(
            "account_overview",
            ACCOUNT_OVERVIEW_QUERY,
            _account_variables,
        ),
        ReadOnlyOperation(
            "account_agreements",
            ACCOUNT_AGREEMENTS_QUERY,
            _agreement_variables,
        ),
        ReadOnlyOperation(
            "account_billing",
            ACCOUNT_BILLING_QUERY,
            _account_variables,
        ),
    )
}


def build_context(*, hours: int, now: datetime) -> ProbeContext:
    """Read local-only probe inputs from the environment and a bounded window."""
    if hours < 1:
        raise ValueError("Probe window must be at least one hour")
    end_at = now.astimezone(UTC)
    return ProbeContext(
        account_number=os.environ.get(ACCOUNT_NUMBER_ENV) or None,
        supply_point_spin=os.environ.get(SUPPLY_POINT_ENV) or None,
        start_at=end_at - timedelta(hours=hours),
        end_at=end_at,
    )


def first_discovered_target(discovery: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return the first account number and supply-point SPIN from discovery data."""
    accounts = ((discovery.get("viewer") or {}).get("accounts")) or []
    for account in accounts:
        number = account.get("number")
        for prop in account.get("properties") or []:
            for point in prop.get("electricitySupplyPoints") or []:
                if spin := point.get("spin"):
                    return number, spin
        if number:
            return number, None
    return None, None


def resolve_context(context: ProbeContext, discovery: Mapping[str, Any]) -> ProbeContext:
    """Fill unset targets from discovery, keeping any explicit override."""
    account_number, supply_point_spin = first_discovered_target(discovery)
    return replace(
        context,
        account_number=context.account_number or account_number,
        supply_point_spin=context.supply_point_spin or supply_point_spin,
    )


async def _authorization_header(session: ClientSession) -> str:
    if header := os.environ.get("OEJP_AUTHORIZATION_HEADER"):
        return header

    email = os.environ.get("OEJP_EMAIL")
    password = os.environ.get("OEJP_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Set OEJP_AUTHORIZATION_HEADER, or OEJP_EMAIL and OEJP_PASSWORD "
            "for the isolated legacy probe"
        )
    token = await async_obtain_token(OejpGraphQLClient(session), email, password)
    return f"JWT {token.access_token}"


async def _fetch(
    operation: ReadOnlyOperation,
    context: ProbeContext,
) -> dict[str, object]:
    async with ClientSession() as session:
        client = OejpGraphQLClient(session)
        header = await _authorization_header(session)
        variables = None
        if operation.variables is not None:
            # The account number and SPIN are discoverable, so the operator never
            # has to retype a customer identifier. An environment override still
            # wins, which is how a second account or supply point is reached.
            if context.account_number is None or context.supply_point_spin is None:
                discovery = await client.execute(
                    LEGACY_DISCOVERY_QUERY,
                    authorization_header=header,
                )
                context = resolve_context(context, discovery)
            variables = operation.variables(context)
        response = await client.execute(
            operation.query,
            variables,
            authorization_header=header,
        )
    return build_contract_fixture(
        operation.name,
        operation.query,
        response,
        source="authorized-local-read-only-probe",
    )


def _write_fixture(
    output: Path,
    fixture: Mapping[str, object],
    *,
    force: bool,
) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite {output}; pass --force")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _graphql_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def main() -> None:
    """Fetch one allow-listed operation and write its synthetic fixture."""
    parser = argparse.ArgumentParser(
        description="Create a sanitized OEJP GraphQL contract fixture."
    )
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help="reading window ending now, in hours (default: %(default)s)",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    context = build_context(hours=args.hours, now=datetime.now(UTC))
    fixture = asyncio.run(_fetch(OPERATIONS[args.operation], context))
    _write_fixture(args.output, fixture, force=args.force)


if __name__ == "__main__":
    main()
