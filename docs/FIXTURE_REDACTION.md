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

Until OAuth is available, a developer may use the deprecated Kraken login only
inside this local probe:

```bash
read -rp 'OEJP email: ' OEJP_EMAIL
read -rsp 'OEJP password: ' OEJP_PASSWORD
export OEJP_EMAIL OEJP_PASSWORD
python scripts/oejp_probe.py viewer_accounts /tmp/viewer_accounts.json
unset OEJP_EMAIL OEJP_PASSWORD
```

Do not place credentials in shell history, command-line arguments, `.env`
files, issue reports, or repository files. The commands above read values
interactively so the values are not part of the command line, and unset them
immediately after the probe. The Home Assistant config flow and runtime never
use this legacy login path.

## Safety model

- The CLI exposes fixed read-only operations and accepts no arbitrary GraphQL.
- Raw responses remain in memory and are never intentionally logged or written.
- Known credential, identity, address, meter, account, and supply-point fields
  are replaced with deterministic per-document synthetic placeholders.
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
