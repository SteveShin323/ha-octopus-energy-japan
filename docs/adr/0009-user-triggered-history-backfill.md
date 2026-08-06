# 0009. A user-triggered walk back through history

## Status

Accepted. Supersedes in part [ADR 0004](0004-non-blocking-runtime-synchronization.md), which
excluded a long backfill from the *bootstrap*. That exclusion still holds: setup collects the
current and previous month and nothing more. This adds a separate, explicitly requested walk.

## Context

Setup collects two months. Measured against a real account this session, the provider keeps
everything: intervals were present for every window back to the day supply began, 48 days on
that account, and nothing suggested a retention limit. The 1488-interval cap turned out to bind
one *response* rather than a range — the legacy query stops 31 days back however wide the
window is, while the paginated generic query returned every interval asked for, 2,292 of them
across 24 pages for a 48-day window.

So the readings exist and were not being collected. For a user who wants years of history on
the Energy Dashboard, that was the most visible thing missing.

Two measurements shaped the design. A reading request costs a flat **17 points** of a
**50,000-per-hour** allowance whatever page size it asks for, and `rateLimitInfo` reports what
is left. And the expensive part of a window is not the request: publishing statistics reads the
whole ledger, and rebuilding the snapshot re-reads every enabled supply point.

## Decision

**A button, not a setting.** A stored boolean would re-trigger on every reload, and un-ticking
to re-tick is a strange way to retry. The walk takes hours and is worth doing once.

**A cursor, not a plan.** Five years is hundreds of windows per direction. Registering them
would grow the checkpoint without bound and make every save quadratic, because each completion
is matched against its generation's windows on every write. One `DirectionBackfill` per
direction records how far back it has reached; the window in flight is derived from it.
Re-fetching a window costs nothing, because the ledger is keyed.

The reason value is never serialized, and the walk is never named in `generations`, so a
checkpoint written by a build with this feature still loads on one without it, and the reverse.

**Stop at the reported supply start, and loop until dry for everything else.**
`supplyPeriods.supplyStartAt` — the earliest billable period, which the billing anchor already
reads — says where an account's readings begin, so a walk that reaches it has nothing older to
collect. `AgreementSummary.valid_from` is *not* used for this: it moves later on a product
switch, so it would silently truncate exactly the customers it would harm.

The empty-window rule stays, because the supply start does not cover two cases: an account that
cannot read that field at all — it is refused through the viewer path, so some accounts have
none — and the gap between two supply periods for a customer who moved out and back in, which
`supplyPeriods` being a list makes possible and the earliest start says nothing about. Three
consecutive empty windows — 21 days of silence — end the walk; one is not evidence, because a
meter exchange or a provider gap each produce one.

`BACKFILL_MAX_HISTORY` bounds both, so a supply start the provider reports wrongly cannot send
the walk to 1970.

**Paced by what the provider says is left.** One window every three seconds draws about a third
of the allowance; below a 20,000-point reserve the walk waits for the reset. Both reuse the
retry controller's existing barrier, which already composes with backoff and already lets a
lower-priority ready item overtake a held one.

**Nothing is projected until the walk ends.** A walked window flushes the ledger and moves the
cursor. It does not project statistics, rebuild the snapshot, or wake the entities — each of
those is what would make hundreds of windows unusable.

The two edges of the walk do all three, once each: the press, so that starting is visible, and
whatever ends it — complete, refused, or failed — so that the collected history reaches the
Energy Dashboard immediately. Deferring the end to the ordinary poll was the first design, and
it was wrong for the same reason the rest of this is right: the walk already costs the user
hours, and a further half hour of a button that looks like it did nothing is not a cost worth
saving one snapshot for. The saving was never in the edges; it is in the hundreds of windows
between them. Neither edge reaches the provider — the snapshot is built from ledgers already
on disk — so the whole walk still costs one projection and two snapshots.

**A legacy answer stops the walk.** That path returns the most recent 31 days however far back
it is asked, so advancing on its answer would record coverage the account does not have and then
declare a month a complete history. The direction stops with its cursor intact and a repair
message says why.

## Consequences

- A five-year walk over both directions is roughly 2,080 requests and about 1.7 hours.
- Collected history reaches the ledger and the Energy Dashboard statistics. It does **not**
  reach the `today` / `this week` / `this month` / `last month` sensors, which aggregate only
  the current and previous month.
- A restart while a walk is in progress costs one whole-ledger projection.
- An entry left unloaded for months leaves a gap in the middle that neither the daily
  reconciliation's two-month window nor this walk fills. Filling it would need multi-window
  planning again. Documented rather than solved.
- An installation that never presses the button behaves exactly as before, which a test pins.
