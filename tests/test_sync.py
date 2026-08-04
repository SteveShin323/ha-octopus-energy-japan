"""Polling, reconciliation, backfill, cadence, and backoff planner tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest
from custom_components.octopus_energy_japan.sync import (
    MAX_QUERY_WINDOW,
    SyncReason,
    SyncScheduleState,
    SyncWindow,
    SyncWindowPlanner,
    exponential_backoff,
    slow_cadence_due,
    startup_stagger,
)

NOW = datetime(2026, 7, 29, 12, 34, tzinfo=UTC)


def test_regular_poll_is_exact_72_hour_overlap() -> None:
    windows = SyncWindowPlanner().poll(NOW)

    assert windows == (
        SyncWindow(
            NOW - timedelta(hours=72),
            NOW,
            SyncReason.POLL,
        ),
    )


@pytest.mark.parametrize(
    ("method", "reason"),
    [
        ("initial", SyncReason.INITIAL),
        ("reconciliation", SyncReason.DAILY_RECONCILIATION),
    ],
)
def test_current_and_previous_jst_month_are_chunked(
    method: str,
    reason: SyncReason,
) -> None:
    windows = getattr(SyncWindowPlanner(), method)(NOW)

    assert windows[0].start_at == datetime(2026, 5, 31, 15, tzinfo=UTC)
    assert windows[-1].end_at == NOW
    assert all(window.reason is reason for window in windows)
    assert all(window.end_at - window.start_at <= MAX_QUERY_WINDOW for window in windows)
    assert all(left.end_at == right.start_at for left, right in pairwise(windows))


def test_every_planned_window_fits_inside_one_provider_response() -> None:
    """OEJP narrows an oversized window silently, which would delete history.

    A `halfHourlyReadings` response was measured on a real account at roughly
    1476 half-hour intervals, about 30.75 days, with no error and no pagination
    marker. An authoritative snapshot deletes stored intervals that fall inside
    the requested window and are absent from the response, so a chunk larger than
    one response can return would discard valid local history.
    """
    provider_response_intervals = 1476
    half_hour = timedelta(minutes=30)
    planner = SyncWindowPlanner()

    planned = (
        *planner.poll(NOW),
        *planner.initial(NOW),
        *planner.reconciliation(NOW),
        *planner.long_backfill(NOW, months=13),
    )

    assert planned
    for window in planned:
        intervals = (window.end_at - window.start_at) / half_hour
        assert intervals <= provider_response_intervals, window
    assert MAX_QUERY_WINDOW / half_hour <= provider_response_intervals


def test_long_backfill_is_bounded_to_requested_local_months() -> None:
    planner = SyncWindowPlanner()
    one_month = planner.long_backfill(NOW, months=1)
    thirteen_months = planner.long_backfill(NOW)

    assert one_month[0].start_at == datetime(2026, 6, 30, 15, tzinfo=UTC)
    assert thirteen_months[0].start_at == datetime(2025, 6, 30, 15, tzinfo=UTC)
    assert thirteen_months[-1].end_at == NOW
    assert all(window.reason is SyncReason.LONG_BACKFILL for window in thirteen_months)

    with pytest.raises(ValueError, match="between 1 and 13"):
        planner.long_backfill(NOW, months=0)
    with pytest.raises(ValueError, match="between 1 and 13"):
        planner.long_backfill(NOW, months=14)


def test_custom_chunk_limit_and_window_invariants() -> None:
    planner = SyncWindowPlanner(max_query_window=timedelta(days=1))
    assert len(planner.poll(NOW)) == 3
    with pytest.raises(ValueError, match="between zero"):
        SyncWindowPlanner(max_query_window=timedelta(0))
    with pytest.raises(ValueError, match="between zero"):
        SyncWindowPlanner(max_query_window=timedelta(days=8))
    with pytest.raises(ValueError, match="later"):
        SyncWindow(NOW, NOW, SyncReason.POLL)
    with pytest.raises(ValueError, match="seven-day"):
        SyncWindow(NOW - timedelta(days=8), NOW, SyncReason.POLL)
    with pytest.raises(ValueError, match="timezone-aware"):
        SyncWindow(
            datetime(2026, 7, 1),  # noqa: DTZ001 - invalid-input test
            NOW,
            SyncReason.POLL,
        )


def test_slow_cadence_is_independent_and_jst_daily() -> None:
    state = SyncScheduleState(
        last_reconciliation_date=date(2026, 7, 29),
        last_discovery_at=NOW - timedelta(hours=23),
        last_contract_at=NOW - timedelta(hours=12),
        last_billing_at=NOW - timedelta(hours=11),
    )

    due = slow_cadence_due(NOW, state)

    assert due.discovery is False
    assert due.contract is True
    assert due.billing is False
    assert due.reconciliation is False

    after_jst_midnight = datetime(2026, 7, 29, 15, 1, tzinfo=UTC)
    due = slow_cadence_due(after_jst_midnight, SyncScheduleState())
    assert due.discovery is True
    assert due.contract is True
    assert due.billing is True
    assert due.reconciliation is True


def test_slow_cadence_rejects_naive_previous_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        slow_cadence_due(
            NOW,
            SyncScheduleState(
                last_discovery_at=datetime(2026, 7, 1),  # noqa: DTZ001
            ),
        )


def test_backoff_is_bounded_deterministic_and_honors_retry_after() -> None:
    first = exponential_backoff(3, jitter_seed="entry")
    second = exponential_backoff(3, jitter_seed="entry")
    capped = exponential_backoff(100, jitter_seed="entry")

    assert first == second
    assert timedelta(0) <= first <= timedelta(minutes=4)
    assert timedelta(0) <= capped <= timedelta(hours=1)
    assert exponential_backoff(
        1,
        retry_after=timedelta(hours=2),
    ) == timedelta(hours=1)

    with pytest.raises(ValueError, match="attempt"):
        exponential_backoff(-1)
    with pytest.raises(ValueError, match="positive"):
        exponential_backoff(1, base=timedelta(0))
    with pytest.raises(ValueError, match="positive"):
        exponential_backoff(1, maximum=timedelta(0))
    with pytest.raises(ValueError, match="Retry-After"):
        exponential_backoff(1, retry_after=timedelta(seconds=-1))


def test_startup_stagger_is_stable_private_and_bounded() -> None:
    first = startup_stagger("installation-hmac")
    second = startup_stagger("installation-hmac")

    assert first == second
    assert timedelta(0) <= first <= timedelta(minutes=5)
    assert startup_stagger(
        "installation-hmac",
        maximum=timedelta(seconds=1),
    ) <= timedelta(seconds=1)

    with pytest.raises(ValueError, match="identity"):
        startup_stagger("")
    with pytest.raises(ValueError, match="maximum"):
        startup_stagger("id", maximum=timedelta(0))


def test_planner_rejects_naive_or_inverted_internal_plan() -> None:
    planner = SyncWindowPlanner()
    with pytest.raises(ValueError, match="timezone-aware"):
        planner.poll(datetime(2026, 7, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="later"):
        planner._chunk(NOW, NOW, SyncReason.POLL)
