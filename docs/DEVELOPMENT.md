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

Coverage must be at least 95% line and branch overall. Authentication, ledger,
statistics, and storage-migration modules must be fully covered. `manifest.json` and
`pyproject.toml` must agree on the version; a test asserts it.

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

## Releases

Versions are `MAJOR.MINOR.PATCH`, with the stage carried by the range rather than a
suffix: `0.1.x` alpha, `0.5.x` and `0.8.x` beta, `1.0.0` once migrations, security,
documentation, translations, and quality gates are all met.

No release is tagged unless:

- every required check passes on the release commit — Validate, Security, CodeQL, and
  Dependency Review;
- coverage meets the thresholds above;
- no `quality_scale.yaml` rule is `todo` that the release stage claims to have met;
- English and Japanese translations have identical key sets;
- the README and every document under `docs/` agree with the code. A status table that
  calls something planned when it is implemented blocks the release; and
- `CHANGELOG.md` has an entry for the version.

## Brand images

`custom_components/octopus_energy_japan/brand/` holds the six images
`home-assistant/brands` requires — `icon`, `logo`, and `dark_logo`, each with its `@2x` —
at the sizes that repository accepts. A test pins every dimension, because a submission
with the wrong one is rejected and the pull request against another repository is the only
place that would otherwise surface it.

They sit **inside** the component even though Home Assistant serves brand images from
`brands.home-assistant.io`. HACS validation looks for
`custom_components/<domain>/brand/icon.png` first and falls back to querying the brands
repository; this integration is not listed there yet, so the in-component copy is what
keeps the `hacs` check passing.

Submitting them — copy the directory to `custom_integrations/octopus_energy_japan/` in a
fork of `home-assistant/brands` — is the one outstanding `quality_scale.yaml` rule and
needs this repository to be public first.

Tag `vMAJOR.MINOR.PATCH` on `main` and publish a GitHub release whose notes are the
changelog entry. HACS installs the `custom_components/octopus_energy_japan` directory
from the tag, so the manifest version and the tag must match.

A security fix is released as a patch on the affected minor version and disclosed
through [`SECURITY.md`](../SECURITY.md).
