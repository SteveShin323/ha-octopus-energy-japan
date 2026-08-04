# Release process

## Version scheme

`MAJOR.MINOR.PATCH`, with the stage carried by the range rather than a suffix:

| Range | Meaning |
|---|---|
| `0.1.x` | alpha. OAuth, discovery, readings |
| `0.5.x` | beta. Ledger, entities, Energy Dashboard, diagnostics |
| `0.8.x` | beta. Import/export, agreements, billing |
| `1.0.0` | migrations, security, documentation, translations, quality gates all met |

`manifest.json` and `pyproject.toml` must agree. A test asserts it.

## Release gates

No release is tagged unless all of these hold.

**Automated**

- every required check passes on the release commit: Validate (Ruff, strict mypy,
  pytest with branch coverage, Hassfest, HACS, documentation links), Security,
  CodeQL, Dependency Review;
- line and branch coverage is at least 95 percent overall, and 100 percent for
  authentication, ledger, statistics, and migration modules;
- no `todo` remains in `quality_scale.yaml` that the release stage claims to have
  met; and
- English and Japanese translations have identical key sets.

**Documentation**

- the README, the master design, and every scoped contract agree with the code. A
  status table claiming something is "planned" when it is implemented blocks the
  release;
- Japanese user documentation is regenerated for the release; and
- `CHANGELOG.md` has an entry for the version.

**Provider**

- an OAuth client ID exists, and OEJP has confirmed in writing that it may be
  published and shared across installations;
- no client secret was issued or required. A reply that grants publication *and*
  issues a secret is self-contradictory and blocks the release until resolved;
- the scopes actually granted to the application match `READ_ONLY_SCOPES` in
  `oauth_metadata.py` string for string. A mismatch fails the authorize request
  with `invalid_scope` before the user reaches a consent screen;
- `https://my.home-assistant.io/redirect/oauth` is the registered redirect URI;
- the response record in [`OAUTH_APPLICATION_STATUS.md`](OAUTH_APPLICATION_STATUS.md)
  has no `Pending` row that the release depends on; and
- any provider behaviour the release relies on was observed, not assumed. A
  contract derived only from documentation is not enough.

**Real-account matrix**

Run against a real OEJP account before tagging:

- first OAuth connection, then access-token refresh, then refresh-token rotation;
- a configuration without `my` loaded aborts with `my_home_assistant_required`
  rather than reaching a provider error page;
- Home Assistant restart recovery;
- multiple accounts and multiple supply points;
- generic and legacy reading providers, including the fallback path;
- import and export;
- a delayed reading, then a corrected reading, then the resulting statistics
  rewrite;
- partial permission, for example agreements forbidden;
- diagnostics redaction, verified by reading the downloaded file;
- HACS clean install, upgrade from the previous release, and removal; and
- Energy Dashboard shows the expected series.

## Tagging

1. update `CHANGELOG.md`;
2. bump the version in `manifest.json` and `pyproject.toml` in one commit;
3. merge to `main` and confirm every check passes there;
4. create an annotated tag `vMAJOR.MINOR.PATCH`;
5. publish a GitHub release whose notes are the changelog entry; and
6. confirm HACS offers the new version.

## HACS packaging

HACS installs `custom_components/octopus_energy_japan/` from the tag. `hacs.json`
declares the minimum Home Assistant version, which must match the lowest version
CI tests against.

The release must contain `manifest.json`, `strings.json`, `icons.json`,
`quality_scale.yaml`, `translations/en.json`, `translations/ja.json`, and the brand
assets. No test, fixture, or development file ships.

## Security releases

A security fix may skip the feature gates but not the automated ones. Follow
[`../SECURITY.md`](../SECURITY.md), release from a minimal change, and state the
impact in the changelog without publishing exploit detail before users can upgrade.

## After a release

Watch for a repair issue or diagnostics pattern that appears across several
installations; that is usually a provider change rather than a local fault. Record
any newly observed provider behaviour in
[`API_CONTRACTS.md`](API_CONTRACTS.md) with the date it was observed.
