# Contributing

Thank you for helping improve the unofficial Octopus Energy Japan integration for Home Assistant.

## Development principles

- Treat the official OEJP GraphQL documentation and observed API fixtures as the source of truth.
- Keep GraphQL transport, authentication, parsing, persistence, aggregation, and Home Assistant entities separated.
- Never select the first account, property, meter, or supply point implicitly.
- Preserve timezone-aware timestamps, reading versions, units, direction, quality, and source metadata.
- Do not expose credentials, account numbers, supply-point identifiers, meter serial numbers, addresses, or billing details in logs, entity states, fixtures, or diagnostics.
- Add tests for behavior changes and update documentation for user-visible changes.

## Local setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
```

Run the validation suite:

```bash
ruff check .
ruff format --check .
mypy custom_components/octopus_energy_japan/api
pytest --cov
```

## Pull requests

Keep pull requests focused. Describe what changed, why it changed, how it was tested, and whether it affects stored data, entity identifiers, GraphQL queries, or user configuration.

Changes to persisted formats require a migration and migration tests. Changes to GraphQL operations require fixture-based parser tests. New entities require documented state semantics and privacy review.

## Commit and release policy

The project follows semantic versioning once public releases begin. Breaking config-entry, storage, or entity-identity changes require a major-version decision or a transparent migration path.
