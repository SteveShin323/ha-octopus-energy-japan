## What changed

<!-- What this does, in plain terms. -->

## Why

<!-- The problem it solves. If it fixes an issue, link it. -->

## Developer impact

<!-- New boundary, changed contract, or behaviour a future change must preserve.
     Delete if there is none. -->

## Verification

<!-- Run against Python 3.14 with .venv3142, matching CI. Paste the outcome. -->

- [ ] `ruff check custom_components tests` and `ruff format --check custom_components tests`
- [ ] `mypy custom_components/octopus_energy_japan`
- [ ] `pytest --cov`, with coverage at or above 95 percent overall
- [ ] every module this PR changes is at 100 percent coverage
- [ ] documentation links resolve

## Contract and privacy

- [ ] no provider behaviour is asserted that was not observed or published. A shape
      taken from documentation alone is labelled as unverified
- [ ] the normative document for the affected scope is updated in this PR, and no
      other document now contradicts it
- [ ] `quality_scale.yaml` reflects what is actually implemented
- [ ] English and Japanese translations have identical key sets
- [ ] no account number, supply-point number, address, token, reading value, or
      monetary amount is added to code, tests, fixtures, documentation, or this
      description
