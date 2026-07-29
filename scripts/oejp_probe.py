#!/usr/bin/env python3
"""Run allow-listed OEJP read operations and emit only synthetic fixtures."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aiohttp import ClientSession
from custom_components.octopus_energy_japan.api import (
    CAPABILITY_QUERY,
    LEGACY_DISCOVERY_QUERY,
    OejpGraphQLClient,
)
from custom_components.octopus_energy_japan.api.operations import (
    VIEWER_ACCOUNTS_QUERY,
    VIEWER_IDENTITY_QUERY,
    async_obtain_token,
)
from custom_components.octopus_energy_japan.probe import build_contract_fixture


@dataclass(frozen=True, slots=True)
class ReadOnlyOperation:
    """One fixed read-only probe operation."""

    name: str
    query: str


OPERATIONS: Final = {
    operation.name: operation
    for operation in (
        ReadOnlyOperation("viewer_identity", VIEWER_IDENTITY_QUERY),
        ReadOnlyOperation("viewer_accounts", VIEWER_ACCOUNTS_QUERY),
        ReadOnlyOperation("resource_discovery", LEGACY_DISCOVERY_QUERY),
        ReadOnlyOperation("schema_capabilities", CAPABILITY_QUERY),
    )
}


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


async def _fetch(operation: ReadOnlyOperation) -> dict[str, object]:
    async with ClientSession() as session:
        client = OejpGraphQLClient(session)
        response = await client.execute(
            operation.query,
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
    fixture: dict[str, object],
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a sanitized OEJP GraphQL contract fixture."
    )
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    fixture = asyncio.run(_fetch(OPERATIONS[args.operation]))
    _write_fixture(args.output, fixture, force=args.force)


if __name__ == "__main__":
    main()
