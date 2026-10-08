"""Unit tests for DailyEnergyRolloverGuard (daily_energy_guard.py).

These exercise the guard in isolation, against a fake client, so we can
precisely control what getSystemPowerOnCurrentDayBatch "returns" without
hitting the network or depending on coordinator/HA wiring.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.apsystems_cloud_crawler.api import (
    ApsystemsCloudCrawlerConnectionError,
)
from custom_components.apsystems_cloud_crawler.daily_energy_guard import (
    DailyEnergyRolloverGuard,
)

DE_KEYS = ["DE0", "DE1", "DE2", "DE3", "DE4", "DE5"]


def _all(value: float | None) -> dict[str, float | None]:
    return dict.fromkeys(DE_KEYS, value)


class FakeClient:
    """Stands in for ApsystemsCloudCrawlerClient's batch endpoint."""

    def __init__(self, batch_by_day: dict[str, dict | None] | None = None, error=None):
        self.batch_by_day = batch_by_day or {}
        self.error = error
        self.calls: list[str] = []

    async def async_get_power_on_current_day_batch(self, day: str):
        self.calls.append(day)
        if self.error is not None:
            raise self.error
        return self.batch_by_day.get(day)


@pytest.mark.asyncio
async def test_first_poll_passes_through_raw_values():
    """No history yet: nothing to compare against, so values pass straight through."""
    guard = DailyEnergyRolloverGuard()
    client = FakeClient()
    result = await guard.async_filter(client, _all(10.0), "2026-10-07 12:00:00", datetime.now(UTC))
    assert result == _all(10.0)
    assert client.calls == []  # no corroboration needed when nothing decreased


@pytest.mark.asyncio
async def test_monotonic_increase_passes_through():
    guard = DailyEnergyRolloverGuard()
    client = FakeClient()
    now = datetime.now(UTC)
    await guard.async_filter(client, _all(10.0), "2026-10-07 12:00:00", now)
    result = await guard.async_filter(client, _all(10.5), "2026-10-07 12:01:00", now)
    assert result == _all(10.5)
    assert client.calls == []


@pytest.mark.asyncio
async def test_transient_glitch_is_discarded_when_series_still_full():
    """A spurious near-zero dip, corroborated away by a full current-day series."""
    guard = DailyEnergyRolloverGuard()
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    # Portal's current-day series still has lots of points -> not a real rollover.
    client = FakeClient({"20261007": {"time": list(range(200))}})

    await guard.async_filter(client, _all(23.5), "2026-10-07 11:55:00", now)
    glitch_result = await guard.async_filter(
        client, _all(0.0), "2026-10-07 12:00:00", now + timedelta(seconds=60)
    )

    # The dip is held back; previous (higher) value keeps being published.
    assert glitch_result == _all(23.5)
    assert client.calls == ["20261007"]

    # Next cycle recovers near the pre-dip level; guard should not re-flag it
    # as a drop (and the published value progresses normally again).
    recovered = await guard.async_filter(
        client, _all(23.6), "2026-10-07 12:01:00", now + timedelta(seconds=120)
    )
    assert recovered == _all(23.6)


@pytest.mark.asyncio
async def test_genuine_rollover_is_confirmed_by_small_series():
    """A real midnight reset: the new day's series has few/no points so far."""
    guard = DailyEnergyRolloverGuard()
    now = datetime(2026, 10, 7, 23, 0, 0, tzinfo=UTC)
    client = FakeClient({"20261008": {"time": [1, 2, 3]}})

    await guard.async_filter(client, _all(24.0), "2026-10-07 22:55:00", now)
    result = await guard.async_filter(
        client, _all(0.1), "2026-10-08 00:00:00", now + timedelta(minutes=65)
    )

    # Confirmed genuine rollover: new (lower) value is published immediately.
    assert result == _all(0.1)
    assert client.calls == ["20261008"]


@pytest.mark.asyncio
async def test_day_string_derived_from_portals_own_report_time_not_local_clock():
    """Corroboration uses the portal's own lastReportTime date, not `now`'s date.

    This is what makes detection correct even when the portal's day
    boundary doesn't line up with Home Assistant's local midnight/timezone.
    """
    guard = DailyEnergyRolloverGuard()
    # `now` is passed in as if HA's clock were a full day further along
    # than the portal's own self-reported time - the guard must still key
    # off the portal's date, not `now`'s.
    now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=UTC)
    client = FakeClient({"20261008": {"time": []}})

    await guard.async_filter(client, _all(24.0), "2026-10-07 23:55:00", now)
    result = await guard.async_filter(client, _all(0.0), "2026-10-08 00:00:00", now)

    assert result == _all(0.0)
    assert client.calls == ["20261008"]


@pytest.mark.asyncio
async def test_inconclusive_result_holds_value_without_hammering_portal():
    """A connection error during corroboration holds the value, not crash, and
    does not retry on every single poll (respects the recheck backoff)."""
    guard = DailyEnergyRolloverGuard()
    client = FakeClient(error=ApsystemsCloudCrawlerConnectionError("boom"))
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)

    await guard.async_filter(client, _all(24.0), "2026-10-07 11:55:00", now)
    held = await guard.async_filter(
        client, _all(0.0), "2026-10-07 12:00:00", now + timedelta(seconds=60)
    )
    assert held == _all(24.0)
    assert len(client.calls) == 1

    # Still within the recheck backoff window: no second network call made.
    held_again = await guard.async_filter(
        client, _all(0.0), "2026-10-07 12:00:00", now + timedelta(seconds=90)
    )
    assert held_again == _all(24.0)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_force_accepts_after_timeout_if_never_corroborated():
    """A persistent drop that can never be corroborated is force-accepted
    after the fail-safe timeout, so a sensor can't be stuck forever."""
    guard = DailyEnergyRolloverGuard()
    client = FakeClient(error=ApsystemsCloudCrawlerConnectionError("boom"))
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)

    await guard.async_filter(client, _all(24.0), "2026-10-07 11:55:00", now)
    await guard.async_filter(client, _all(0.0), "2026-10-07 12:00:00", now)

    # Far past the force-accept timeout (15 minutes).
    result = await guard.async_filter(
        client, _all(0.0), "2026-10-07 12:20:00", now + timedelta(minutes=20)
    )
    assert result == _all(0.0)


@pytest.mark.asyncio
async def test_independent_keys_are_not_held_back_by_an_unrelated_drop():
    """Only the actually-decreased keys are held; others keep progressing."""
    guard = DailyEnergyRolloverGuard()
    now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    client = FakeClient({"20261007": {"time": list(range(200))}})

    first = dict.fromkeys(DE_KEYS, 10.0)
    await guard.async_filter(client, first, "2026-10-07 11:55:00", now)

    second = dict(first)
    second["DE2"] = 0.0  # only DE2 glitches
    second["DE4"] = 10.5  # DE4 keeps growing normally
    result = await guard.async_filter(
        client, second, "2026-10-07 12:00:00", now + timedelta(seconds=60)
    )

    assert result["DE2"] == 10.0  # held back
    assert result["DE4"] == 10.5  # unaffected, progressed normally
    assert result["DE0"] == 10.0  # unchanged, still fine
