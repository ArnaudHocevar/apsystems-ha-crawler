"""Unit tests for DailyEnergyRolloverMonitor (daily_energy_monitor.py).

These exercise the monitor in isolation against a fake client, verifying
it only observes/logs and never computes a value to be published.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.apsystems_cloud_crawler.api import (
    ApsystemsCloudCrawlerConnectionError,
)
from custom_components.apsystems_cloud_crawler.daily_energy_monitor import (
    DailyEnergyRolloverMonitor,
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
async def test_first_poll_logs_nothing(caplog: pytest.LogCaptureFixture):
    """No history yet: nothing to compare against, so there's nothing to log."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient()
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(
            client, _all(10.0), "2026-10-07 12:00:00", {}, datetime.now(UTC)
        )
    assert caplog.records == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_monotonic_increase_logs_nothing(caplog: pytest.LogCaptureFixture):
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient()
    now = datetime.now(UTC)
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(10.0), "2026-10-07 12:00:00", {}, now)
        await monitor.async_observe(client, _all(10.5), "2026-10-07 12:01:00", {}, now)
    assert caplog.records == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_small_fluctuation_within_epsilon_logs_nothing(caplog: pytest.LogCaptureFixture):
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient()
    now = datetime.now(UTC)
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(10.0001), "2026-10-07 12:00:00", {}, now)
        await monitor.async_observe(client, _all(10.0), "2026-10-07 12:01:00", {}, now)
    assert caplog.records == []


@pytest.mark.asyncio
async def test_decrease_to_near_zero_logged_as_likely_rollover(
    caplog: pytest.LogCaptureFixture,
):
    """A clean flush to ~0 is logged, but framed as expected/genuine rollover."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient(batch_by_day={"20261008": {"time": [1]}})
    now = datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    live_context = {"grid_power": 150, "pv_power": 0, "load_power": 150}
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(23.5), "2026-10-07 23:59:00", {}, now)
        await monitor.async_observe(
            client, _all(0.1), "2026-10-08 00:01:00", live_context, now
        )
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert "genuine portal day rollover" in message
    assert "grid_power" in message or "live" in message
    assert client.calls == ["20261008"]


@pytest.mark.asyncio
async def test_decrease_not_near_zero_logged_as_suspicious(caplog: pytest.LogCaptureFixture):
    """A drop that doesn't flush to near-zero is logged more loudly."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient(batch_by_day={"20261007": {"time": list(range(400))}})
    now = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(23.5), "2026-10-07 13:59:00", {}, now)
        await monitor.async_observe(
            client, {**_all(23.5), "DE0": 10.0}, "2026-10-07 14:00:00", {}, now
        )

    assert len(caplog.records) == 1
    assert "may indicate a portal data inconsistency" in caplog.records[0].message


@pytest.mark.asyncio
async def test_values_always_pass_through_unaltered():
    """The monitor never returns anything - callers keep publishing raw values."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient()
    now = datetime.now(UTC)
    result = await monitor.async_observe(
        client, _all(23.5), "2026-10-07 13:59:00", {}, now
    )
    assert result is None  # observe-only: nothing to override with


@pytest.mark.asyncio
async def test_corroboration_is_rate_limited(caplog: pytest.LogCaptureFixture):
    """Repeated decreases within the recheck window don't re-hit the portal."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient(batch_by_day={"20261007": {"time": [1, 2]}})
    now = datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(23.5), "2026-10-07 13:59:00", {}, now)
        await monitor.async_observe(client, _all(0.0), "2026-10-07 14:00:00", {}, now)
        await monitor.async_observe(client, _all(23.5), "2026-10-07 14:01:00", {}, now)
        await monitor.async_observe(
            client, _all(0.0), "2026-10-07 14:02:00", {}, now + timedelta(minutes=1)
        )
    assert client.calls == ["20261007"]
    assert len(caplog.records) == 2
    assert "skipped (rechecked recently)" in caplog.records[1].message


@pytest.mark.asyncio
async def test_corroboration_failure_is_tolerated(caplog: pytest.LogCaptureFixture):
    """A failed corroboration call must never raise, just annotate as unavailable."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient(error=ApsystemsCloudCrawlerConnectionError("boom"))
    now = datetime.now(UTC)
    with caplog.at_level(logging.WARNING):
        await monitor.async_observe(client, _all(23.5), "2026-10-07 13:59:00", {}, now)
        await monitor.async_observe(client, _all(0.0), "2026-10-07 14:00:00", {}, now)
    assert len(caplog.records) == 1
    assert "unavailable (request failed)" in caplog.records[0].message


@pytest.mark.asyncio
async def test_tz_independence_uses_portal_reported_date():
    """The day string used for corroboration comes from the portal's own timestamp."""
    monitor = DailyEnergyRolloverMonitor()
    client = FakeClient(batch_by_day={"20261008": {"time": []}})
    now = datetime(2026, 10, 7, 23, 0, tzinfo=UTC)  # HA's "now" is still Oct 7
    await monitor.async_observe(client, _all(23.5), "2026-10-07 23:59:00", {}, now)
    # Portal says it's already Oct 8 locally (e.g. CET ahead of UTC)
    await monitor.async_observe(client, _all(0.0), "2026-10-08 00:05:00", {}, now)
    assert client.calls == ["20261008"]
