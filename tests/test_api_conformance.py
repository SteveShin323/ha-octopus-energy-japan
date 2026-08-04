"""Every shipped GraphQL query must obey the provider's documented API limits.

The limits are stated in OEJP's own GraphQL guide at
`https://docs.oejp-kraken.energy/graphql/guides/basics/`, read 2026-08-04:

- a paginated field's `first` argument "must be set to a value less than 100";
  a request without it, or over the limit, errors;
- query complexity is capped at 200 per request (`KT-CT-1188`); and
- a request may return at most 10,000 nodes (`KT-CT-1189`).

These are scanned rather than asserted per query, so a query added later is
covered without anyone remembering to extend this file. `devices` and `registers`
shipped without `first` until this was written.

The scan covers module-level query constants. The generic reading query is built at
runtime and passes `first` as a GraphQL variable, so its page size is bounded by the
`page_size` validation in `OejpGenericReadingProvider.__init__` instead, which is
asserted separately in `test_api_readings.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from custom_components.octopus_energy_japan.api.models import MAX_PAGE_SIZE

API = Path(__file__).parents[1] / "custom_components" / "octopus_energy_japan" / "api"

# A rendered query constant: an uppercase module-level name holding a triple-quoted
# string that contains a GraphQL operation.
_QUERY_CONSTANT = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*)\s*=\s*f?\"\"\"(?P<body>.*?)\"\"\"",
    re.MULTILINE | re.DOTALL,
)
_FIRST_ARGUMENT = re.compile(r"\bfirst:\s*(?P<value>\d+)")


def _rendered_queries() -> list[tuple[str, str, str]]:
    """Return (module, constant name, rendered query) for every shipped query."""
    import importlib

    found: list[tuple[str, str, str]] = []
    for path in sorted(API.glob("*.py")):
        module = importlib.import_module(f"custom_components.octopus_energy_japan.api.{path.stem}")
        for match in _QUERY_CONSTANT.finditer(path.read_text(encoding="utf-8")):
            name = match.group("name")
            value = getattr(module, name, None)
            if isinstance(value, str) and ("query " in value or "mutation " in value):
                found.append((path.stem, name, value))
    return found


def _connection_fields(query: str) -> list[tuple[str, str]]:
    """Return (field name, argument text) for each Relay connection selection.

    A connection is recognised by an `edges` or `pageInfo` child, which is what
    distinguishes it from the provider's plain list fields such as
    `halfHourlyReadings`. Those take no `first` and must not be flagged.
    """
    connections: list[tuple[str, str]] = []
    for match in re.finditer(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*(\([^()]*(?:\([^()]*\)[^()]*)*\))?\s*\{", query
    ):
        field, arguments = match.group(1), match.group(2) or ""
        depth, index = 0, match.end() - 1
        while index < len(query):
            if query[index] == "{":
                depth += 1
            elif query[index] == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        body = query[match.end() : index]
        # Only direct children matter: a nested connection has its own iteration,
        # and an outer wrapper such as `account` must not inherit its child's
        # `edges`. Strip nested blocks repeatedly until only direct children remain.
        direct = body
        while True:
            stripped = re.sub(r"\{[^{}]*\}", "", direct)
            if stripped == direct:
                break
            direct = stripped
        if re.search(r"\b(edges|pageInfo)\b", direct):
            connections.append((field, arguments))
    return connections


def test_at_least_one_query_and_one_connection_are_scanned() -> None:
    """Guard against the scan silently matching nothing and passing vacuously."""
    queries = _rendered_queries()
    assert len(queries) >= 8
    assert any(_connection_fields(query) for _, _, query in queries)


@pytest.mark.parametrize(("module", "name", "query"), _rendered_queries())
def test_every_connection_requests_a_conforming_page_size(
    module: str,
    name: str,
    query: str,
) -> None:
    for field, arguments in _connection_fields(query):
        where = f"{module}.{name}: {field}"
        match = _FIRST_ARGUMENT.search(arguments)
        assert match is not None, f"{where} is a connection without `first`"
        page_size = int(match.group("value"))
        assert 1 <= page_size <= MAX_PAGE_SIZE, f"{where} requests first: {page_size}"


@pytest.mark.parametrize(("module", "name", "query"), _rendered_queries())
def test_no_query_requests_a_page_size_at_or_over_the_documented_limit(
    module: str,
    name: str,
    query: str,
) -> None:
    """Covers `first` wherever it appears, including non-connection arguments."""
    for match in _FIRST_ARGUMENT.finditer(query):
        page_size = int(match.group("value"))
        assert page_size < 100, f"{module}.{name} requests first: {page_size}"
        assert page_size <= MAX_PAGE_SIZE


def test_documented_maximum_page_size_is_the_largest_conforming_value() -> None:
    assert MAX_PAGE_SIZE == 99
