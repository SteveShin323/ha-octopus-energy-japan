# ADR 0003: Persist interval readings in a correction-aware ledger

Status: accepted
Date: 2026-07-29

## Context

OEJP readings may arrive late or be revised. Restoring and incrementing a single
total, or storing only the last synchronization cursor, cannot reliably handle
duplicates, corrections, missing intervals, restarts, or historical
reconciliation.

## Decision

Persist normalized interval readings in versioned monthly Home Assistant Store
partitions. Identify a record by series plus UTC start and end time. Preserve
value, revision, quality, official cost, source, and fetch metadata.

Derive aggregates and Home Assistant external statistics from the ledger.
Reproject statistics from the earliest affected hour after a correction.

## Consequences

- Results are reproducible and idempotent.
- Storage migration and damaged-partition recovery become required features.
- Current and previous months can stay in memory while older data loads lazily.
- Ledger merge and statistics projection are critical modules with 100% test
  coverage targets and property-based tests.
