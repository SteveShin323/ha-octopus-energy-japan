"""Background retry controller tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.octopus_energy_japan.api import ReadingDirection
from custom_components.octopus_energy_japan.background_sync import (
    BackgroundSyncItem,
    BackgroundSyncReason,
    BackgroundSyncScope,
    BackgroundWindow,
    SyncObligation,
)
from custom_components.octopus_energy_japan.sync_runtime import (
    BACKGROUND_DEFER,
    BackgroundRetryController,
)

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)


def _item(suffix: str = "a") -> BackgroundSyncItem:
    scope = BackgroundSyncScope(
        "supply-point-" + suffix * 64,
        ReadingDirection.IMPORT,
        BackgroundWindow(NOW - timedelta(days=7), NOW),
    )
    return BackgroundSyncItem(
        scope,
        frozenset(
            {
                SyncObligation(
                    BackgroundSyncReason.INITIAL_CURRENT_MONTH,
                    f"generation-{suffix}",
                )
            }
        ),
    )


def test_item_local_retry_does_not_block_unrelated_ready_work() -> None:
    controller = BackgroundRetryController()
    blocked = _item("a")
    ready = _item("b")
    decision = controller.record_transient(
        blocked.scope,
        NOW,
        retry_after=timedelta(minutes=5),
        rate_limited=False,
    )

    assert decision.not_before == NOW + timedelta(minutes=5)
    assert controller.available((blocked, ready), NOW).item == ready
    assert controller.available((blocked,), NOW).not_before == decision.not_before


def test_rate_limit_sets_entry_wide_not_before() -> None:
    controller = BackgroundRetryController()
    first = _item("a")
    second = _item("b")
    decision = controller.record_transient(
        first.scope,
        NOW,
        retry_after=timedelta(minutes=10),
        rate_limited=True,
    )

    assert controller.entry_not_before == decision.not_before
    assert controller.available((second,), NOW).item is None
    assert controller.available((second,), decision.not_before).item == second


def test_fifth_transient_failure_defers_six_hours_and_resets_activation() -> None:
    controller = BackgroundRetryController()
    item = _item()
    decisions = [
        controller.record_transient(
            item.scope,
            NOW,
            retry_after=timedelta(0),
            rate_limited=False,
        )
        for _ in range(5)
    ]

    assert [value.activation_attempt for value in decisions] == [1, 2, 3, 4, 0]
    assert decisions[-1].deferred
    assert decisions[-1].not_before == NOW + BACKGROUND_DEFER


def test_resolve_and_prune_remove_obsolete_retry_state() -> None:
    controller = BackgroundRetryController()
    first = _item("a")
    second = _item("b")
    for item in (first, second):
        controller.record_transient(
            item.scope,
            NOW,
            retry_after=timedelta(minutes=1),
            rate_limited=False,
        )

    controller.resolve(first.scope)
    assert controller.available((first,), NOW).item == first
    controller.prune(frozenset())
    assert controller.available((second,), NOW).item == second


def test_retry_controller_rejects_naive_clock() -> None:
    controller = BackgroundRetryController()
    with pytest.raises(ValueError, match="timezone-aware"):
        controller.available((_item(),), datetime(2026, 7, 29))  # noqa: DTZ001


def test_a_paced_scope_yields_to_a_ready_one_of_lower_priority() -> None:
    """Pacing reuses the retry barrier, so `available` already knows how to skip it."""
    controller = BackgroundRetryController()
    paced = _item("a")
    ready = _item("b")
    controller.defer(paced.scope, NOW + timedelta(seconds=3))

    available = controller.available((paced, ready), NOW)

    assert available.item is ready


def test_pacing_never_moves_a_backoff_earlier() -> None:
    """A three-second pace must not shorten an hour of exponential backoff."""
    controller = BackgroundRetryController()
    item = _item()
    schedule = controller.record_transient(
        item.scope,
        NOW,
        retry_after=timedelta(hours=1),
        rate_limited=False,
    )

    controller.defer(item.scope, NOW + timedelta(seconds=3))

    assert controller.available((item,), NOW).not_before == schedule.not_before


def test_a_scope_that_was_only_paced_is_still_pruned() -> None:
    """A paced scope has no attempt count, so iterating those alone leaked its barrier."""
    controller = BackgroundRetryController()
    item = _item()
    controller.defer(item.scope, NOW + timedelta(hours=1))

    controller.prune(frozenset())

    assert controller.available((item,), NOW).item is item
