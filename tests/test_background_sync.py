"""Persistent background queue, generation, checkpoint, and coverage tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.octopus_energy_japan.api import ReadingDirection
from custom_components.octopus_energy_japan.background_sync import (
    BackgroundSyncItem,
    BackgroundSyncPlanner,
    BackgroundSyncPriority,
    BackgroundSyncQueue,
    BackgroundSyncReason,
    BackgroundSyncScope,
    BackgroundWindow,
    CoverageWindow,
    DailyDirectionCompletion,
    DirectionWindowCompletion,
    PlannedGeneration,
    SyncCheckpoint,
    SyncObligation,
)

NOW = datetime(2026, 7, 29, 12, 34, tzinfo=UTC)
POINT_A = "supply-point-" + "a" * 64
POINT_B = "supply-point-" + "b" * 64


def _scope(
    point: str = POINT_A,
    direction: ReadingDirection = ReadingDirection.IMPORT,
    *,
    start: datetime = NOW - timedelta(days=7),
    end: datetime = NOW,
) -> BackgroundSyncScope:
    return BackgroundSyncScope(point, direction, BackgroundWindow(start, end))


def _obligation(
    reason: BackgroundSyncReason = BackgroundSyncReason.INITIAL_CURRENT_MONTH,
    generation: str = "generation",
) -> SyncObligation:
    return SyncObligation(reason, generation)


def test_queue_coalesces_scope_and_upgrades_priority() -> None:
    queue = BackgroundSyncQueue()
    scope = _scope()
    initial = _obligation()
    daily = _obligation(BackgroundSyncReason.DAILY_RECONCILIATION, "daily")

    queue.enqueue(scope, initial)
    queue.enqueue(scope, daily)

    assert len(queue) == 1
    item = queue.snapshot()[0]
    assert item.obligations == {initial, daily}
    assert item.priority is BackgroundSyncPriority.DAILY_RECONCILIATION


def test_queue_orders_priority_newest_point_and_direction() -> None:
    queue = BackgroundSyncQueue()
    older = _scope(start=NOW - timedelta(days=14), end=NOW - timedelta(days=7))
    newer_a_import = _scope()
    newer_a_export = _scope(direction=ReadingDirection.EXPORT)
    newer_b = _scope(POINT_B)
    for scope in (older, newer_b, newer_a_import, newer_a_export):
        queue.enqueue(scope, _obligation())
    queue.enqueue(
        older,
        _obligation(BackgroundSyncReason.DAILY_RECONCILIATION, "daily"),
    )

    assert [item.scope for item in queue.snapshot()] == [
        older,
        newer_a_export,
        newer_a_import,
        newer_b,
    ]
    assert queue.pop_next().scope == older  # type: ignore[union-attr]
    assert len(queue) == 3


def test_removing_obsolete_obligation_retains_shared_scope() -> None:
    queue = BackgroundSyncQueue()
    scope = _scope()
    initial = _obligation(generation="old")
    daily = _obligation(BackgroundSyncReason.DAILY_RECONCILIATION, "daily")
    queue.enqueue(scope, initial)
    queue.enqueue(scope, daily)

    queue.remove_obligations(
        BackgroundSyncReason.INITIAL_CURRENT_MONTH,
        frozenset({"old"}),
    )

    assert queue.snapshot()[0].obligations == {daily}
    queue.discard(scope)
    assert queue.pop_next() is None


def test_popped_item_can_be_restored_without_losing_obligations() -> None:
    queue = BackgroundSyncQueue()
    item = BackgroundSyncItem(
        _scope(),
        frozenset(
            {
                _obligation(),
                _obligation(BackgroundSyncReason.DAILY_RECONCILIATION, "daily"),
            }
        ),
    )

    queue.enqueue_item(item)
    assert queue.pop_next() == item


def test_initial_planner_excludes_poll_window_and_orders_months_newest_first() -> None:
    plans = BackgroundSyncPlanner().initial(NOW)

    assert [plan.obligation.reason for plan in plans] == [
        BackgroundSyncReason.INITIAL_CURRENT_MONTH,
        BackgroundSyncReason.INITIAL_PREVIOUS_MONTH,
    ]
    cutoff = NOW - timedelta(hours=72)
    assert plans[0].target_end == cutoff
    assert plans[0].windows[0].end_at == cutoff
    assert plans[0].windows[-1].start_at == datetime(2026, 6, 30, 15, tzinfo=UTC)
    assert plans[1].target_end == datetime(2026, 6, 30, 15, tzinfo=UTC)
    assert plans[1].windows[0].end_at == plans[1].target_end
    assert all(
        left.start_at >= right.start_at
        for plan in plans
        for left, right in zip(plan.windows, plan.windows[1:], strict=False)
    )
    assert all(window.end_at <= cutoff for plan in plans for window in plan.windows)


def test_initial_planner_handles_early_month_without_current_history() -> None:
    now = datetime(2026, 8, 1, 10, tzinfo=UTC)
    plans = BackgroundSyncPlanner().initial(now)

    assert [plan.obligation.reason for plan in plans] == [
        BackgroundSyncReason.INITIAL_PREVIOUS_MONTH
    ]
    assert plans[0].target_end == now - timedelta(hours=72)


def test_daily_planner_has_exact_target_and_jst_generation() -> None:
    plan = BackgroundSyncPlanner().daily(NOW)

    assert plan.target_end == NOW
    assert plan.jst_date.isoformat() == "2026-07-29"  # type: ignore[union-attr]
    assert plan.obligation.reason is BackgroundSyncReason.DAILY_RECONCILIATION
    assert plan.obligation.generation.endswith("20260729T123400Z")
    assert plan.windows[0].end_at == NOW
    assert plan.windows[-1].start_at == datetime(2026, 5, 31, 15, tzinfo=UTC)


def test_partial_daily_checkpoint_survives_round_trip_and_restart() -> None:
    planner = BackgroundSyncPlanner()
    daily = planner.daily(NOW)
    checkpoint = SyncCheckpoint.empty(NOW).register(daily)
    first_scope = BackgroundSyncScope(
        POINT_A,
        ReadingDirection.IMPORT,
        daily.windows[0],
    )
    checkpoint = checkpoint.mark_durable(
        BackgroundSyncItem(first_scope, frozenset({daily.obligation}))
    )

    restored = SyncCheckpoint.from_dict(checkpoint.as_dict())
    queue = BackgroundSyncQueue()
    restored.enqueue_missing(queue, POINT_A, ReadingDirection.IMPORT, daily)

    assert restored.is_completed(
        ReadingDirection.IMPORT,
        daily.obligation,
        daily.windows[0],
    )
    assert len(queue) == len(daily.windows) - 1
    assert restored.daily_completed == ()


def test_daily_barriers_advance_independently_by_direction() -> None:
    daily = BackgroundSyncPlanner().daily(NOW)
    checkpoint = SyncCheckpoint.empty(NOW).register(daily)
    for window in daily.windows:
        checkpoint = checkpoint.mark_durable(
            BackgroundSyncItem(
                BackgroundSyncScope(POINT_A, ReadingDirection.IMPORT, window),
                frozenset({daily.obligation}),
            )
        )

    assert checkpoint.daily_completed == (
        DailyDirectionCompletion(
            ReadingDirection.IMPORT,
            daily.jst_date,  # type: ignore[arg-type]
            NOW,
        ),
    )
    export_queue = BackgroundSyncQueue()
    checkpoint.enqueue_missing(
        export_queue,
        POINT_A,
        ReadingDirection.EXPORT,
        daily,
    )
    assert len(export_queue) == len(daily.windows)


def test_authoritative_empty_completion_merges_adjacent_coverage() -> None:
    checkpoint = SyncCheckpoint.empty(NOW)
    obligation = _obligation()
    first = BackgroundWindow(NOW - timedelta(days=14), NOW - timedelta(days=7))
    second = BackgroundWindow(NOW - timedelta(days=7), NOW)
    generation = PlannedGeneration(obligation, NOW, (second, first))
    checkpoint = checkpoint.register(generation)
    for window in (first, second):
        checkpoint = checkpoint.mark_durable(
            BackgroundSyncItem(
                BackgroundSyncScope(POINT_A, ReadingDirection.IMPORT, window),
                frozenset({obligation}),
            )
        )

    assert checkpoint.coverage_for(ReadingDirection.IMPORT) == (
        CoverageWindow(NOW - timedelta(days=14), NOW),
    )


def test_month_rollover_removes_only_initial_metadata() -> None:
    initial = BackgroundSyncPlanner().initial(NOW)[0]
    daily = BackgroundSyncPlanner().daily(NOW)
    checkpoint = SyncCheckpoint.empty(NOW).register(initial).register(daily)
    initial_item = BackgroundSyncItem(
        BackgroundSyncScope(POINT_A, ReadingDirection.IMPORT, initial.windows[0]),
        frozenset({initial.obligation}),
    )
    daily_item = BackgroundSyncItem(
        BackgroundSyncScope(POINT_A, ReadingDirection.IMPORT, daily.windows[0]),
        frozenset({daily.obligation}),
    )
    checkpoint = checkpoint.mark_durable(initial_item).mark_durable(daily_item)

    rolled = checkpoint.roll_month_pair(datetime(2026, 8, 31, 16, tzinfo=UTC))

    assert rolled.month_pair_generation == "2026-08_2026-09"
    assert [value.obligation.reason for value in rolled.generations] == [
        BackgroundSyncReason.DAILY_RECONCILIATION
    ]
    assert all(
        value.reason is BackgroundSyncReason.DAILY_RECONCILIATION
        for value in rolled.completed_windows
    )
    assert rolled.background_coverage == checkpoint.background_coverage
    assert rolled.roll_month_pair(datetime(2026, 9, 1, tzinfo=UTC)) is rolled


def test_checkpoint_rejects_invalid_schema_and_payload() -> None:
    payload = SyncCheckpoint.empty(NOW).as_dict()
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match="schema version"):
        SyncCheckpoint.from_dict(payload)

    with pytest.raises(ValueError, match="timestamp"):
        BackgroundWindow(datetime(2026, 7, 1), NOW)  # noqa: DTZ001
    with pytest.raises(ValueError, match="later"):
        BackgroundWindow(NOW, NOW)
    with pytest.raises(ValueError, match="later"):
        CoverageWindow(NOW, NOW)
    with pytest.raises(ValueError, match="seven-day"):
        BackgroundWindow(NOW - timedelta(days=8), NOW)
    with pytest.raises(ValueError, match="opaque"):
        _scope("raw-provider-id")
    with pytest.raises(ValueError, match="direction"):
        _scope(direction=ReadingDirection.UNKNOWN)
    with pytest.raises(ValueError, match="generation"):
        SyncObligation(BackgroundSyncReason.LONG_BACKFILL, "")
    with pytest.raises(ValueError, match="obligation"):
        BackgroundSyncItem(_scope(), frozenset())

    checkpoint = SyncCheckpoint.empty(NOW)
    with pytest.raises(ValueError, match="registered generation"):
        checkpoint.mark_durable(BackgroundSyncItem(_scope(), frozenset({_obligation()})))

    with pytest.raises(ValueError, match="month-pair"):
        SyncCheckpoint("invalid")
    with pytest.raises(ValueError, match="schema version"):
        SyncCheckpoint("2026-06_2026-07", schema_version=2)


def test_checkpoint_rejects_inconsistent_generation_metadata() -> None:
    obligation = _obligation()
    generation = PlannedGeneration(obligation, NOW, (_scope().window,))
    duplicate = (generation, generation)
    with pytest.raises(ValueError, match="duplicate generations"):
        SyncCheckpoint("2026-06_2026-07", generations=duplicate)

    completion = DirectionWindowCompletion(
        ReadingDirection.IMPORT,
        obligation.reason,
        obligation.generation,
        _scope().window,
    )
    with pytest.raises(ValueError, match="matching generation"):
        SyncCheckpoint("2026-06_2026-07", completed_windows=(completion,))

    with pytest.raises(ValueError, match="Daily generation"):
        PlannedGeneration(
            SyncObligation(BackgroundSyncReason.DAILY_RECONCILIATION, "daily"),
            NOW,
            (_scope().window,),
        )
    with pytest.raises(ValueError, match="Only daily"):
        PlannedGeneration(obligation, NOW, (_scope().window,), NOW.date())
    with pytest.raises(ValueError, match="Daily supersession"):
        SyncCheckpoint.empty(NOW).supersede_daily(generation)


def test_generation_identifier_cannot_be_reused_for_different_windows() -> None:
    obligation = _obligation()
    first = PlannedGeneration(obligation, NOW, (_scope().window,))
    second_window = BackgroundWindow(NOW - timedelta(days=6), NOW)
    second = PlannedGeneration(obligation, NOW, (second_window,))

    with pytest.raises(ValueError, match="reused"):
        SyncCheckpoint.empty(NOW).register(first).register(second)


def test_checkpoint_deserialization_rejects_malformed_nested_values() -> None:
    valid = SyncCheckpoint.empty(NOW).as_dict()
    malformed_payloads = (
        {**valid, "generations": ["not-an-object"]},
        {
            **valid,
            "generations": [
                {
                    "reason": BackgroundSyncReason.INITIAL_CURRENT_MONTH.value,
                    "generation": "test",
                    "target_end": "2026-07-29T12:34:00Z",
                    "jst_date": 123,
                    "windows": [],
                }
            ],
        },
        {**valid, "completed_windows": ["not-an-object"]},
        {**valid, "completed_windows": "not-a-list"},
        {**valid, "month_pair_generation": ""},
        {
            **valid,
            "background_coverage": [
                {
                    "direction": ReadingDirection.UNKNOWN.value,
                    "start_at": "2026-07-01T00:00:00Z",
                    "end_at": "2026-07-02T00:00:00Z",
                }
            ],
        },
        {
            **valid,
            "background_coverage": [
                {
                    "direction": ReadingDirection.IMPORT.value,
                    "start_at": "not-a-date",
                    "end_at": "2026-07-02T00:00:00Z",
                }
            ],
        },
    )

    for payload in malformed_payloads:
        with pytest.raises((ValueError, TypeError)):
            SyncCheckpoint.from_dict(payload)


def test_removing_only_obligation_deletes_queue_item() -> None:
    queue = BackgroundSyncQueue()
    obligation = _obligation(generation="obsolete")
    queue.enqueue(_scope(), obligation)

    queue.remove_obligations(obligation.reason, frozenset({obligation.generation}))

    assert len(queue) == 0


def test_permanent_failure_round_trip_prevents_same_generation_spin() -> None:
    obligation = _obligation()
    generation = PlannedGeneration(obligation, NOW, (_scope().window,))
    item = BackgroundSyncItem(_scope(), frozenset({obligation}))

    failed = SyncCheckpoint.empty(NOW).register(generation).mark_failed(item, "authorization")
    restored = SyncCheckpoint.from_dict(failed.as_dict())
    queue = BackgroundSyncQueue()
    restored.enqueue_missing(
        queue,
        POINT_A,
        ReadingDirection.IMPORT,
        generation,
    )

    assert restored.is_failed(
        ReadingDirection.IMPORT,
        obligation,
        generation.windows[0],
    )
    assert queue.snapshot() == ()


def test_permanent_failure_requires_registered_window_and_safe_class() -> None:
    item = BackgroundSyncItem(_scope(), frozenset({_obligation()}))
    with pytest.raises(ValueError, match="registered generation"):
        SyncCheckpoint.empty(NOW).mark_failed(item, "authorization")

    generation = PlannedGeneration(_obligation(), NOW, (_scope().window,))
    with pytest.raises(ValueError, match="failure class"):
        SyncCheckpoint.empty(NOW).register(generation).mark_failed(item, "")

    failure_payload = SyncCheckpoint.empty(NOW).register(generation).as_dict()
    failure_payload["failed_windows"] = [
        {
            "direction": "import",
            "reason": generation.obligation.reason.value,
            "generation": generation.obligation.generation,
            "start_at": (NOW - timedelta(days=6)).isoformat(),
            "end_at": NOW.isoformat(),
            "error_class": "authorization",
        }
    ]
    with pytest.raises(ValueError, match="failure has no matching"):
        SyncCheckpoint.from_dict(failure_payload)


def test_checkpoint_without_failure_field_restores_backward_compatibly() -> None:
    payload = SyncCheckpoint.empty(NOW).as_dict()
    payload.pop("failed_windows")

    assert SyncCheckpoint.from_dict(payload).failed_windows == ()
