"""Unit tests for the synthesized lifetime energy tracker (lifetime_energy.py).

These exercise ``advance_tracker`` (the pure state-transition function) and
the restore-state payload round trip directly, without needing a running
Home Assistant instance - the entity class itself is a thin wrapper around
these that only does I/O (coordinator data, restore state, logging).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.apsystems_cloud_crawler.const import LIFETIME_SUSPICIOUS_HOLD_TIMEOUT
from custom_components.apsystems_cloud_crawler.lifetime_energy import (
    _LifetimeExtraStoredData,
    _TrackerState,
    advance_tracker,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_first_observation_just_baselines_without_adding():
    state = _TrackerState(accumulated=100.0)  # e.g. restored from a prior run
    new_state, status = advance_tracker(state, 23.5, "20261009", NOW)
    assert status == "baseline"
    assert new_state.accumulated == 100.0
    assert new_state.last_raw == 23.5
    assert new_state.last_portal_day == "20261009"


def test_normal_increase_adds_the_delta():
    state = _TrackerState(accumulated=10.0, last_raw=5.0, last_portal_day="20261009")
    new_state, status = advance_tracker(state, 7.5, "20261009", NOW)
    assert status == "increase"
    assert new_state.accumulated == 12.5
    assert new_state.last_raw == 7.5


def test_tiny_fluctuation_within_epsilon_does_not_regress():
    state = _TrackerState(accumulated=10.0, last_raw=5.0001, last_portal_day="20261009")
    new_state, status = advance_tracker(state, 5.0, "20261009", NOW)
    assert status == "increase"
    assert new_state.accumulated == 10.0  # delta clamped to 0, no negative addition
    assert new_state.last_raw == 5.0


def test_decrease_with_portal_day_change_is_confirmed_rollover():
    state = _TrackerState(accumulated=23.5, last_raw=23.5, last_portal_day="20261008")
    new_state, status = advance_tracker(state, 0.1, "20261009", NOW)
    assert status == "confirmed_rollover"
    # Yesterday's growth was already folded in incrementally; only the new
    # day's own progress-so-far gets added on top.
    assert new_state.accumulated == 23.6
    assert new_state.last_raw == 0.1
    assert new_state.last_portal_day == "20261009"


def test_decrease_without_day_change_is_held_as_suspicious():
    state = _TrackerState(accumulated=23.5, last_raw=23.5, last_portal_day="20261009")
    new_state, status = advance_tracker(state, 0.1, "20261009", NOW)
    assert status == "suspicious_held"
    # Nothing added, baseline kept at the higher pre-glitch value so a
    # recovery on the next poll isn't double-counted.
    assert new_state.accumulated == 23.5
    assert new_state.last_raw == 23.5
    assert new_state.held_since == NOW


def test_recovery_after_held_glitch_does_not_double_count():
    state = _TrackerState(accumulated=23.5, last_raw=23.5, last_portal_day="20261009")
    held_state, status = advance_tracker(state, 0.1, "20261009", NOW)
    assert status == "suspicious_held"
    recovered_state, status = advance_tracker(
        held_state, 23.6, "20261009", NOW + timedelta(minutes=1)
    )
    assert status == "increase"
    assert recovered_state.accumulated == 23.6  # only the genuine +0.1 growth, no double count
    assert recovered_state.held_since is None


def test_held_glitch_force_accepted_after_timeout():
    state = _TrackerState(
        accumulated=23.5, last_raw=23.5, last_portal_day="20261009", held_since=NOW
    )
    later = NOW + LIFETIME_SUSPICIOUS_HOLD_TIMEOUT
    new_state, status = advance_tracker(state, 0.1, "20261009", later)
    assert status == "suspicious_force_accepted"
    assert new_state.accumulated == 23.5  # the gap itself is never added, just unstuck
    assert new_state.last_raw == 0.1
    assert new_state.held_since is None


def test_held_glitch_not_yet_force_accepted_before_timeout():
    state = _TrackerState(
        accumulated=23.5, last_raw=23.5, last_portal_day="20261009", held_since=NOW
    )
    still_holding = NOW + LIFETIME_SUSPICIOUS_HOLD_TIMEOUT - timedelta(seconds=1)
    new_state, status = advance_tracker(state, 0.1, "20261009", still_holding)
    assert status == "suspicious_held"
    assert new_state.last_raw == 23.5


def test_missing_day_string_does_not_crash_and_is_treated_as_suspicious():
    state = _TrackerState(accumulated=23.5, last_raw=23.5, last_portal_day=None)
    new_state, status = advance_tracker(state, 0.1, None, NOW)
    assert status == "suspicious_held"
    assert new_state.accumulated == 23.5


def test_none_raw_value_is_a_no_op():
    state = _TrackerState(accumulated=10.0, last_raw=5.0, last_portal_day="20261009")
    new_state, status = advance_tracker(state, None, "20261009", NOW)
    assert status == "no_data"
    assert new_state == state


def test_extra_stored_data_round_trips():
    payload = _LifetimeExtraStoredData(
        accumulated=42.5, last_raw=12.3, last_portal_day="20261009"
    )
    restored = _LifetimeExtraStoredData.from_dict(payload.as_dict())
    assert restored == payload


def test_extra_stored_data_from_dict_handles_malformed_input():
    assert _LifetimeExtraStoredData.from_dict({}) is None
    assert _LifetimeExtraStoredData.from_dict({"accumulated": "not-a-number"}) is None
