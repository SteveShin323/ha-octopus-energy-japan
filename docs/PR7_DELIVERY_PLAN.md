# PR 7 sequential delivery plan

Status: normative branch and review procedure for PR #14
Reviewed: 2026-07-31

This document controls how the remaining PR 7 work is delivered. Runtime
behavior is controlled by [`RUNTIME_AND_ENTITIES.md`](RUNTIME_AND_ENTITIES.md).
The delivery procedure MUST NOT weaken or reinterpret that specification.

## 1. Fixed integration target

- Integration branch: `codex/runtime-entities`
- Integration pull request: PR #14, base `main`
- PR #14 remains Draft until the complete PR 7 definition of done is met.
- No child PR and no automation may merge PR #14 into `main`.
- PR 8 and later feature work MUST NOT start before PR #14 is ready for review.

## 2. Sequential child-PR model

Child PRs are processed one at a time. They are not a parallel stack.

For every stage:

1. ensure PR #14 and `codex/runtime-entities` are synchronized and green;
2. create the stage branch from the current `codex/runtime-entities` head;
3. open a Draft child PR with base `codex/runtime-entities`;
4. implement only that stage plus changes strictly required to preserve existing
   behavior and tests;
5. update tests and normative documents affected by that stage;
6. remove all TODO, FIXME, placeholder, temporary compatibility path, and open
   design choice introduced or exposed in that stage;
7. run every repository check on the child PR;
8. resolve actionable review findings and make the child PR ready for review;
9. squash-merge the child PR into `codex/runtime-entities`, never into `main`;
10. verify every repository check again on the updated PR #14 head; and
11. delete or retire the child branch before creating the next stage branch.

A later stage MUST NOT branch from an unmerged child branch. Two implementation
child PRs MUST NOT remain active simultaneously. If `main` changes materially,
synchronize PR #14 between stages, resolve its CI, and only then start the next
child PR.

All pull-request workflows MUST run for non-`main` base branches. The workflow
configuration on `codex/runtime-entities` is part of the delivery infrastructure
and MUST remain enabled for child PRs.

## 3. Scope discipline

Each child PR description MUST contain:

- exact normative sections implemented;
- files and runtime boundaries intentionally changed;
- invariants and failure modes added;
- mandatory tests added or changed;
- explicit non-goals delegated to later stages; and
- the exact child head SHA validated before merge.

A child PR may refactor adjacent code only when required to complete its own
contract. Broad cleanup, unrelated dependency updates, PR 8 statistics, tariff,
billing, diagnostics, Repairs, and release work are forbidden.

A child PR is not complete merely because CI is green. Its stage acceptance
criteria below must be demonstrably satisfied.

## 4. Delivery stages

### Stage 1 — Gated transport and direction-scoped provider foundation

Suggested branch: `codex/pr7-gated-direction-provider`

Implement:

- the one-logical-operation request gate around authenticated GraphQL execution,
  including the single token-refresh retry;
- typed HTTP status and retry-after propagation;
- removal of runtime access to the ungated transport;
- sequential gated generic topology and reading pagination;
- per-supply-point, per-direction provider results and observations;
- exact candidate-direction rules;
- direction-only generic-to-legacy fallback; and
- removal of the final batch-wide provider-selection path.

Acceptance tests include gate concurrency across refresh/retry, HTTP and
`Retry-After` classification, mixed import/export results, successful empty
export, legacy unknown-direction import-only, and forbidden fallback cases.

### Stage 2 — Bounded setup and regular coordinator

Suggested branch: `codex/pr7-bounded-bootstrap`

Implement:

- the exact setup sequence;
- 72-hour-only first refresh;
- permanent-only status-only setup outcome;
- all-transient retryable setup outcome;
- immutable per-point/direction status in coordinator data;
- narrow partial-failure isolation;
- deterministic regular-poll ordering;
- setup cleanup after partial runtime allocation; and
- aggregation restricted to enabled/queryable states.

No background month work may run during setup. At the end of this stage the
runtime is functional for regular 72-hour polling even though persistent
historical work is not yet complete.

### Stage 3 — Persistent queue, checkpoints, and coverage

Suggested branch: `codex/pr7-persistent-sync-queue`

Implement:

- one background worker per config entry;
- request-scope items and multi-obligation coalescing;
- deterministic priority and newest-first planning;
- versioned per-supply-point checkpoint stores;
- reason/generation/direction completed windows;
- partial and complete daily barriers;
- checkpoint-after-ledger-flush durability;
- persisted background coverage and restart reconstruction;
- current/previous-month initial work excluding the final 72 hours; and
- daily reconciliation generation planning.

Acceptance tests must include crash-safe ordering, partial daily restart,
obligation coalescing, month-generation rollover, empty authoritative windows,
and independent direction history.

### Stage 4 — Priority, retry, cancellation, and unload recovery

Suggested branch: `codex/pr7-runtime-recovery`

Implement:

- explicit poll-pending preemption before the next background request;
- entry-wide rate-limit `not_before`;
- item-local transient retry;
- deterministic full-jitter backoff and retry-after behavior;
- five-attempt/six-hour defer;
- permanent-generation no-spin behavior;
- authentication-driven worker termination and reauthentication;
- idempotent shutdown;
- atomic-section-aware cancellation; and
- unload-failure reconstruction of exactly one worker.

Waiting must hold neither request gate nor ledger/checkpoint mutation lock.

### Stage 5 — Lifecycle, registries, and entity semantics

Suggested branch: `codex/pr7-lifecycle-entities`

Implement:

- every lifecycle transition in the runtime specification;
- historical account and point selection semantics;
- cancellation of disabled-point obligations without deleting stores;
- active aggregation exclusion for disabled/missing resources;
- HMAC identity reuse on reappearance;
- device integration-disable/update behavior;
- status and directional entity availability;
- dynamic direction/entity addition exactly once;
- period coverage gating; and
- complete English/Japanese translations.

Acceptance tests must use the Home Assistant registry and config-entry harness
for transition, reload, unload, and identity continuity behavior.

### Stage 6 — Adversarial completion and documentation closure

Suggested branch: `codex/pr7-completion-audit`

Perform a fresh adversarial review rather than only filling coverage gaps.

Complete:

- the entire mandatory regression matrix;
- multi-account, multi-point, multi-direction, multi-window scenarios;
- restart and corruption-adjacent behavior within PR 7 scope;
- raw-identifier and credential non-exposure assertions;
- elimination of alternate or dead runtime paths;
- consistency of master design, ledger design, ADRs, runtime specification,
  README status, quality-scale claims, comments, and PR descriptions; and
- final formatting, typing, coverage, HACS, Hassfest, link, security, CodeQL,
  Codecov, and dependency-review validation.

Do not mark a quality-scale rule `done` unless the repository contains evidence
and tests for that exact rule. Do not change version numbers or claim supported
installation as part of PR 7.

## 5. Child-PR merge gates

Before each child squash merge:

- its diff is limited to the declared stage;
- every stage acceptance test passes;
- the complete repository test suite passes;
- Ruff, format, strict mypy, coverage, Hassfest, HACS, links, Security, CodeQL,
  Codecov, and dependency review pass on the final child head;
- no actionable review thread remains; and
- the expected head SHA is rechecked immediately before merge.

After merge, the same checks must pass on PR #14 before the next child begins.
A transient external-service failure may be rerun only after its logs show that
repository code and configuration were not the cause.

## 6. Final PR #14 gate

After Stage 6:

1. compare PR #14 against current `main` and synchronize if needed;
2. rerun every required check on the final integration head;
3. audit every MUST/MUST NOT and every mandatory regression test in
   `RUNTIME_AND_ENTITIES.md` against concrete code/tests;
4. confirm no open child PR, temporary branch dependency, TODO, FIXME,
   placeholder, compatibility shim, or unchecked completion item remains;
5. update PR #14 with the completed stage PR references and validation evidence;
6. mark PR #14 ready for review; and
7. stop without merging PR #14 or beginning PR 8.
