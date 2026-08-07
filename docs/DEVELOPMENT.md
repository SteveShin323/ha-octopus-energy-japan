# Development

## Setup

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
```

## Gates

The same checks CI runs, in the same order:

```bash
ruff check custom_components tests
ruff format --check custom_components tests
mypy custom_components/octopus_energy_japan
pytest --cov
```

Re-run the install itself after adding a **top-level directory**. `pyproject.toml` uses
setuptools' flat layout, so a new one at the repository root is discovered as a second
top-level package and the build refuses with "Multiple top-level packages discovered in a
flat-layout". Nothing else catches that: an already-installed environment keeps working, so
it fails first in CI.

Coverage must be at least 95% line and branch overall, which the gate enforces.

Beyond that number, what matters is *which* lines. Authentication, the ledger, statistics,
and storage migration need a test for every correctness path and every concurrency
invariant — anything where being wrong loses or double-counts data. Defensive guards are
not chased to 100%: a `continue` that skips a shape the caller never produces is worth
leaving uncovered rather than reaching with an artificial test.

`manifest.json` and `pyproject.toml` must agree on the version; a test asserts it.

## Pull requests

Keep a pull request to one topic. Say what changed, why, how it was tested, and whether
it affects stored data, entity identity, GraphQL documents, or user configuration.

- A change to a persisted format needs a migration and migration tests.
- A change to a GraphQL document needs fixture-based parser tests.
- A new entity needs documented state semantics and a privacy review.
- A user-visible change needs the README and the Japanese guide updated together.
  English is the normative documentation language; no other translations are maintained.

## Verifying against a real account

`tests/test_live_account.py` drives a real setup and two real refreshes and asserts the
coordinator reaches a healthy state. It skips unless `OEJP_EMAIL` and `OEJP_PASSWORD` are
both set, so CI never runs it:

```bash
OEJP_EMAIL=... OEJP_PASSWORD=... python -m pytest tests/test_live_account.py -s
```

It exists as a committed test rather than a throwaway script because three separate network
blocks have to come off for a request to leave the harness — `pytest-socket`, the test
plugin's `getaddrinfo` replacement, and Home Assistant's shared session, whose DNS resolver
is bound to another event loop. Only the session is substituted; the integration still calls
`async_get_clientsession(hass)` exactly as it does live.

Run it after changing the coordinator, the reading providers, or authentication. The unit
suite proves the rules; this proves a real account still reaches them.

## Live API investigation

The repository never stores live credentials or raw customer responses. Fixtures are
produced locally by an allow-listed, read-only probe and are synthetic before they reach
disk.

```bash
read -r 'OEJP_EMAIL?email: '
read -rs 'OEJP_PASSWORD?password: '
export OEJP_EMAIL OEJP_PASSWORD
python scripts/oejp_probe.py resource_discovery /tmp/discovery.json
unset OEJP_EMAIL OEJP_PASSWORD
```

Those `read` forms are `zsh`, the default macOS shell. In `bash` use
`read -rp 'email: ' OEJP_EMAIL` and `read -rsp 'password: ' OEJP_PASSWORD`.

Credentials are read interactively so they never enter shell history or a command line,
and are unset immediately. Never place them in a command-line argument, a `.env` file,
an issue, or a repository file.

`python scripts/oejp_probe.py --help` lists the allow-listed operations. Reading
operations accept `--hours` or `--ending` to select a window.

### Safety model

Three independent layers, because one is not enough:

1. **Allow-list.** The probe can only send documents defined in the integration, and
   only queries. It cannot send a mutation or an arbitrary document.
2. **Redaction before disk.** Every value under a sensitive key — tokens, account and
   supply point numbers, meter and device identifiers, names, email addresses, addresses,
   postcodes, and monetary amounts — is replaced with a synthetic placeholder. Keys not
   in the list are classified by name, so a new sensitive field is redacted by default
   rather than leaked by omission.
3. **Scanner.** `scripts/scan_fixtures.py` rejects a fixture that still contains a
   plausible real value. CI runs it on every pull request.

Measurements taken against a real account belong in a code comment or in
[`API_CONTRACTS.md`](API_CONTRACTS.md), expressed as a relative statement. Never record a
customer's consumption, invoice amount, account number, or supply point number in the
repository, in a commit message, or in a pull request.

## Home Assistant quality scale

The rule names and tiers are Home Assistant's, taken from `ALL_RULES` in
`script/hassfest/quality_scale.py` in `home-assistant/core` rather than from a summary of a
web page: 20 bronze, 10 silver, 21 gold, 3 platinum.

**A custom integration cannot be awarded a tier.** Home Assistant grades the integrations it
ships; the scale's own documentation puts custom integrations outside it and states that the
project does not review, audit, maintain, or support them. This repository therefore does not
carry a `quality_scale.yaml`: nothing validates one for a custom integration — verified by
putting an invented rule name and an invalid status in the file and watching `hassfest`, `hacs`
and every other job pass — and a file full of self-assigned marks would read as a certification
that was never granted.

What follows is the same rule set used as a checklist, with each verdict and its reason. Gold
is cumulative, so bronze and silver are included.

### Met

Every bronze, silver, and gold rule not listed under *Not applicable* below. The ones worth
knowing where to find:

| Rule | Where |
|---|---|
| `runtime-data` | `entry.runtime_data` holds `OejpRuntimeData`; nothing is kept in `hass.data` |
| `config-flow-test-coverage`, `test-coverage` | `tests/test_config_flow.py`; overall coverage is enforced at 95% and sits near 99% |
| `entity-unique-id`, `has-entity-name` | `entity.py`, from an installation-local HMAC |
| `entity-translations`, `icon-translations` | `strings.json` plus `translations/{en,ja}.json`, and `icons.json` |
| `exception-translations` | every `HomeAssistantError` subclass raised carries `translation_domain` and `translation_key`; three tests keep the keys and messages in step |
| `reauthentication-flow`, `reconfiguration-flow` | `config_flow.py` |
| `repair-issues`, `diagnostics` | `issues.py`, `diagnostics.py` |
| `dynamic-devices` | a coordinator listener adds entities for a supply point that appears later, with no reload |
| `parallel-updates` | `PARALLEL_UPDATES = 0` in every platform; the coordinator owns the requests |
| `log-when-unavailable` | every failure reaches Home Assistant as `UpdateFailed`, which the coordinator logs once and then suppresses until it recovers. Statistics projection, which is outside that path, keeps its own once-per-condition flag |
| `docs-*` | each maps to a README section: `#what-it-does`, `#installation`, `#configuration-parameters`, `#entities`, `#how-data-updates`, `#typical-uses`, `#energy-dashboard`, `#known-limitations`, `#troubleshooting`, `#removing-the-integration` |

### Not applicable, with the reason

| Rule | Why |
|---|---|
| `action-setup`, `action-exceptions`, `docs-actions` | the integration registers no actions and no services. The one control it offers is a button entity, which starts a read |
| `docs-triggers`, `docs-conditions` | no triggers or conditions, for the same reason |
| `entity-event-setup` | nothing subscribes to an external event stream; data arrives by polling |
| `discovery`, `discovery-update-info` | a cloud service reached by account credentials. There is nothing on the network to discover |
| `docs-supported-devices` | no devices are supported in the hardware sense. `README.md#what-it-supports` says what the account exposes instead |

### Deliberately different

**`stale-devices`.** The rule asks that a device no longer provided be removed. A supply point
that ends is **disabled** instead, and can be re-enabled from the integration's options. Ending
a contract should not delete the energy history recorded against it, and Home Assistant offers
no way to keep statistics for a device it has removed. `runtime.py` only ever changes disabling
it set itself, so a user who enables one keeps that choice.

### Platinum

| Rule | State |
|---|---|
| `strict-typing` | mypy runs in `strict` mode over every module and passes. The rule's own validator also requires each `manifest.json` requirement to ship `py.typed`; there are no requirements. Its first check is membership of core's `.strict-typing` file, which a custom integration cannot join |
| `inject-websession` | the client is constructed with `async_get_clientsession(hass)`. It never creates a session |
| `async-dependency` | there is no external client library. `api/` is async throughout and imports nothing from Home Assistant, which `tests/test_documentation_consistency.py` asserts |

## Releases

Versions are `MAJOR.MINOR.PATCH`, with the stage carried by the range rather than a
suffix: `0.1.x` alpha, `0.5.x` through `0.9.x` beta, `1.0.0` onwards stable.

`1.0.0` was reached on 2026-08-06, once migrations, security, documentation, translations,
and the quality gates were all met. An OAuth application had been listed as the last
condition; it was removed as a condition rather than met, because the provider replied that
it will not issue one — see [ADR 0001](adr/0001-oauth-public-client.md). Waiting for it
would have meant never releasing.

`1.0.0` also means the entity IDs, unique IDs, statistic IDs, and stored formats are
settled, because changing one breaks a user's automations or their Energy dashboard. That
is a reason to leave time between the first release and `1.0.0` rather than a box to tick.

No release is tagged unless:

- every required check passes on the release commit — Validate, Security, CodeQL, and
  Dependency Review;
- coverage meets the thresholds above;
- every quality-scale rule below still holds;
- English and Japanese translations have identical key sets;
- the README and every document under `docs/` agree with the code. A status table that
  calls something planned when it is implemented blocks the release; and
- `CHANGELOG.md` has an entry for the version.

## Brand images

`custom_components/octopus_energy_japan/brand/` holds `icon`, `logo`, and `dark_logo`,
each with its `@2x` variant, at the sizes Home Assistant accepts. A test pins every
dimension and requires an alpha channel.

**Nothing needs submitting anywhere.** Since Home Assistant 2026.3 a custom integration
ships its brand images in that directory, and the `custom_integrations` folder of
`home-assistant/brands` is legacy — its own README says so. Moving these files out breaks
three CI jobs, which `tests/test_manifest.py` records.

`dark_icon.png` and `dark_icon@2x.png` are also supported and deliberately absent: the icon
is legible on either theme, so a dark variant would be a second copy of the same image.

Tag `vMAJOR.MINOR.PATCH` on `main` and publish a GitHub release whose notes are the
changelog entry. HACS installs the `custom_components/octopus_energy_japan` directory
from the tag, so the manifest version and the tag must match.

A security fix is released as a patch on the affected minor version and disclosed
through [`SECURITY.md`](../SECURITY.md).
