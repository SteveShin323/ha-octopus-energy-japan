# Live probe and fixture redaction

The repository never stores live OEJP credentials or raw customer responses.
Contract fixtures are produced locally by an allow-listed, read-only probe and
are synthetic before they reach disk.

## Running the probe

Create a virtual environment and install the test dependencies. Prefer an OAuth
authorization header after OEJP confirms the production OAuth contract:

```bash
read -rsp 'OEJP authorization header: ' OEJP_AUTHORIZATION_HEADER
export OEJP_AUTHORIZATION_HEADER
python scripts/oejp_probe.py viewer_accounts /tmp/viewer_accounts.json
unset OEJP_AUTHORIZATION_HEADER
```

In `zsh`, which is the default macOS shell, `read` takes its prompt inside the
variable name and `-p` means something else entirely, so use:

```zsh
read -rs 'OEJP_AUTHORIZATION_HEADER?OEJP authorization header: '
export OEJP_AUTHORIZATION_HEADER
```

Until OAuth is available, a developer may use the deprecated Kraken login only
inside this local probe:

```bash
read -rp 'OEJP email: ' OEJP_EMAIL
read -rsp 'OEJP password: ' OEJP_PASSWORD
export OEJP_EMAIL OEJP_PASSWORD
python scripts/oejp_probe.py viewer_accounts /tmp/viewer_accounts.json
unset OEJP_EMAIL OEJP_PASSWORD
```

The same command in `zsh`:

```zsh
read -r 'OEJP_EMAIL?OEJP email: '
read -rs 'OEJP_PASSWORD?OEJP password: '
export OEJP_EMAIL OEJP_PASSWORD
```

Do not place credentials in shell history, command-line arguments, `.env`
files, issue reports, or repository files. The commands above read values
interactively so the values are not part of the command line, and unset them
immediately after the probe. The Home Assistant config flow and runtime never
use this legacy login path.

The fixed discovery probes used to validate the current customer-visible schema
are:

```bash
python scripts/oejp_probe.py resource_discovery /tmp/resource_discovery.json
python scripts/oejp_probe.py schema_capabilities /tmp/schema_capabilities.json
```

Run every command from the repository root. The script adds the repository root
to `sys.path` itself, so no editable install or `PYTHONPATH` is required.

## Parameterized operations

Reading and commercial operations need an account number or a supply-point SPIN.
The probe **discovers both itself** and keeps them in memory, so no customer
identifier is ever typed, pasted, or placed on a command line.

Set an override only to reach a second account or supply point:

```bash
export OEJP_PROBE_ACCOUNT_NUMBER=... OEJP_PROBE_SUPPLY_POINT_SPIN=...
```

`--hours` sets the window length and defaults to 48. `--ending` moves the window
off the present, which is how a per-response result cap is told apart from a
provider history horizon:

```bash
python scripts/oejp_probe.py legacy_half_hourly_readings /tmp/old.json \
  --ending 2026-06-25T00:00:00Z --hours 168
```

| Operation | Target | Window |
|---|---|---|
| `generic_devices` | SPIN | no |
| `generic_import_readings` | SPIN | yes |
| `generic_export_readings` | SPIN | yes |
| `legacy_half_hourly_readings` | account number | yes |
| `legacy_interval_readings` | account number | yes |
| `account_overview` | account number | no |
| `account_agreements` | account number | no |
| `account_billing` | account number | no |

```bash
python scripts/oejp_probe.py generic_import_readings /tmp/generic_import.json --hours 48
python scripts/oejp_probe.py legacy_half_hourly_readings /tmp/legacy_half_hourly.json
python scripts/oejp_probe.py account_overview /tmp/account_overview.json
python scripts/oejp_probe.py account_agreements /tmp/account_agreements.json
python scripts/oejp_probe.py account_billing /tmp/account_billing.json
unset OEJP_PROBE_ACCOUNT_NUMBER OEJP_PROBE_SUPPLY_POINT_SPIN
```

Only the fixed variable shape above is bound. The CLI still accepts no arbitrary
GraphQL, no arbitrary variables, and no mutations.

## What each probe can settle

`docs/CONTRACT_AND_BILLING.md` records four unmet verification items that keep
provider cost and tariff rates unpublished. These probes are how they close:

| Verification item | Probe | What to look for | Status |
|---|---|---|---|
| Account permission for cost fields | `legacy_half_hourly_readings` | whether `costEstimate` returns a value or an authorization error | closed 2026-08-04 |
| Interval coverage | `legacy_half_hourly_readings` | whether every returned interval carries `costEstimate` | closed 2026-08-04 |
| Currency and denomination | `account_agreements`, then `account_billing` | the `currency` and `unit` on a rate, then whether the bill total reconciles against a bill read from the OEJP web account | open |
| Correction semantics | `legacy_half_hourly_readings`, twice over an overlapping window | whether a revised reading also revises its `costEstimate` and `version` | open |
| OAuth permission for cost fields | any reading probe once OAuth exists | the legacy login is not evidence for account-user OAuth | blocked on OEJP |

The commercial documents were validated by introspection on 2026-08-04, which
corrected the rate fields and the bill fragment aliases. They are no longer
guesses, but a GraphQL validation error from any operation is still a real
finding: record it and correct the document before relying on the parser.

Monetary fields are replaced with placeholders before anything reaches disk, so
reconciling an amount against a real bill requires reading the value from the
OEJP web account, not from the fixture.

## Safety model

- The CLI exposes fixed read-only operations and accepts no arbitrary GraphQL.
- Raw responses remain in memory and are never intentionally logged or written.
- Known credential, identity, address, meter, account, supply-point, and
  monetary-amount fields are replaced with deterministic per-document synthetic
  placeholders. A placeholder is a string, so a fixture cannot be used to assert
  the JSON type of a redacted field.
- Reading values, units, quality, and timestamps are preserved because they carry
  the contract shape rather than an identity. Do not commit a probe fixture whose
  readings you are unwilling to publish; hand-author a `synthetic-test-data`
  fixture from the observed shape instead, as the committed contract fixtures do.
- A second scanner rejects credential patterns, email addresses, unsanitized
  sensitive fields, and any original value registered by the sanitizer.
- Each fixture records the operation, sanitizer/schema version, and SHA-256 of
  the exact query so parser tests have verifiable contract provenance.
- Output is refused when the target already exists unless `--force` is explicit.

Before committing a generated fixture, inspect its shape manually and run:

```bash
pytest tests/test_probe.py
gitleaks detect --no-banner
```

Never commit a fixture if any field's sensitivity is uncertain. Add the field to
the sanitizer, regenerate the fixture, and include a regression test.
