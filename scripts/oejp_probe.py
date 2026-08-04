#!/usr/bin/env python3
"""Run allow-listed OEJP read operations and emit only synthetic fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    GENERIC_MARKET_NAME,
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
        """Return the requested account number or explain how to provide it."""
        if not self.account_number:
            raise RuntimeError(f"Set {ACCOUNT_NUMBER_ENV} for this operation")
        return self.account_number

    def supply_point(self) -> str:
        """Return the requested supply-point SPIN or explain how to provide it."""
        if not self.supply_point_spin:
            raise RuntimeError(f"Set {SUPPLY_POINT_ENV} for this operation")
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
        "marketName": GENERIC_MARKET_NAME,
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
    variables = operation.variables(context) if operation.variables is not None else None
    async with ClientSession() as session:
        client = OejpGraphQLClient(session)
        response = await client.execute(
            operation.query,
            variables,
            authorization_header=await _authorization_header(session),
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
