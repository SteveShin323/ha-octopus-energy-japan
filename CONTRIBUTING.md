# Contributing

Thanks for helping improve this integration.

Start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the code is organised
and which invariants must hold, and [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for
setup, gates, pull request expectations, live API investigation, and releases.

## Principles

- **Do not assert provider behaviour that was not observed.** A shape taken from
  documentation alone is unverified until a probe confirms it. Several defects in this
  project's history came from skipping that.
- Keep authentication, transport, parsing, persistence, aggregation, statistics, and
  Home Assistant entities separated.
- Never implicitly select the first account, property, meter, or supply point.
- Preserve timezone-aware timestamps, reading versions, units, direction, quality, and
  source metadata.
- Never expose a credential, account number, supply point number, meter serial, address,
  or billing detail in a log, entity state, fixture, or diagnostics payload.
- Add tests at the boundary that changed, and update user documentation for anything
  user-visible.

Breaking a config-entry, storage, or entity-identity format requires either a
major-version decision or a transparent migration.

Please also read [`SECURITY.md`](SECURITY.md) and
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
