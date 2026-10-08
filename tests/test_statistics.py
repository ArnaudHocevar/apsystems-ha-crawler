"""Unit tests for statistics.py: the shared range-backfill core used by both
the automatic startup gap-fill and the apsystems_cloud_crawler.backfill service.

These tests mock the recorder statistics functions and the API client's
day-batch fetch, so no real network or Home Assistant recorder instance is
needed.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.statistics import valid_statistic_id

from custom_components.apsystems_cloud_crawler.const import DAILY_ENERGY_SENSORS
from custom_components.apsystems_cloud_crawler.statistics import (
    _statistic_id,
    async_backfill_date_range,
)


def _make_batch(start_value: float, day_offset: int = 0) -> dict:
    """Build a fake getSystemPowerOnCurrentDayBatch response with 2 intervals
    in 2 different hours (so _build_day_statistics's hourly aggregation
    produces 2 distinct hourly points, not 1).

    ``day_offset`` shifts the (fake) interval timestamps by whole days so
    that batches for different calendar days don't collide when compared
    against a previous day's last-written timestamp.
    """
    base_dt = datetime(2026, 1, 1, 10, 0, tzinfo=UTC) + timedelta(
        days=day_offset
    )
    base = int(base_dt.timestamp() * 1000)
    second_hour = base + 3_600_000  # exactly 1 hour later, still top-of-hour
    return {
        "time": [base, second_hour],
        "dischargeEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
        "chargeEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
        "producedEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
        "consumedEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
        "exportedEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
        "importedEnergy": [f"{start_value:.3f}", f"{start_value:.3f}"],
    }


class FakeHass:
    """Minimal stand-in for HomeAssistant: just runs the executor job inline."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.mark.asyncio
async def test_backfill_date_range_skips_missing_day_and_continues():
    """A day with no usable batch response is skipped (not an abort), and
    the other days in the range are still backfilled for every DE key."""
    hass = FakeHass()
    client = AsyncMock()

    # Day 1 and day 3 return usable data; day 2 (the middle day) returns
    # None, simulating a date outside the portal's retention window.
    client.async_get_power_on_current_day_batch.side_effect = [
        _make_batch(1.0, day_offset=0),
        None,
        _make_batch(2.0, day_offset=2),
    ]

    with patch(
        "custom_components.apsystems_cloud_crawler.statistics.get_last_statistics",
        return_value={},
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.async_add_external_statistics"
    ) as mock_add_stats, patch(
        "custom_components.apsystems_cloud_crawler.statistics.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        results = await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 1, 1),
            date(2026, 1, 3),
            pace_seconds=0.5,
        )

    # 3 days requested -> 3 fetch calls, one per day, regardless of DE key
    # count (the batch endpoint returns all six series in one response).
    assert client.async_get_power_on_current_day_batch.call_count == 3

    # Every DE key should have exactly 2 days written (day 1 and day 3; day
    # 2 skipped).
    assert set(results.keys()) == set(DAILY_ENERGY_SENSORS.keys())
    for de_key, days_written in results.items():
        assert days_written == 2, f"{de_key} expected 2 days written, got {days_written}"

    # One async_add_external_statistics call per (day-with-data, DE key):
    # 2 days * 6 keys = 12.
    assert mock_add_stats.call_count == 2 * len(DAILY_ENERGY_SENSORS)

    # Pacing: sleep is called between each day except after the last one,
    # i.e. 2 times for a 3-day range.
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(0.5)


@pytest.mark.asyncio
async def test_backfill_date_range_continues_cumulative_sum_from_last_known():
    """When a prior statistic point exists, the running sum continues from
    it (not from zero), and the already-covered hour is not re-written nor
    double-counted into the running sum of later hours."""
    hass = FakeHass()
    client = AsyncMock()
    client.async_get_power_on_current_day_batch.return_value = _make_batch(1.0)

    statistic_id = _statistic_id("entry123", "DE2")
    # Last known point covers the day's first hour (10:00-11:00 UTC); its
    # "end" (11:00) is exactly the start of the batch's second interval.
    last_end = datetime(2026, 1, 1, 11, 0, tzinfo=UTC)

    def fake_get_last_statistics(hass_arg, count, stat_id, convert, types):
        if stat_id == statistic_id:
            return {stat_id: [{"end": last_end.timestamp(), "sum": 10.0, "state": 1.0}]}
        return {}

    with patch(
        "custom_components.apsystems_cloud_crawler.statistics.get_last_statistics",
        side_effect=fake_get_last_statistics,
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.async_add_external_statistics"
    ) as mock_add_stats, patch(
        "custom_components.apsystems_cloud_crawler.statistics.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        results = await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 1, 1),
            date(2026, 1, 1),
            pace_seconds=0,
        )

    assert results["DE2"] == 1  # only the new (second) hour gets written

    de2_calls = [
        call
        for call in mock_add_stats.call_args_list
        if call.args[1]["statistic_id"] == statistic_id
    ]
    assert len(de2_calls) == 1
    written_points = de2_calls[0].args[2]
    # Only the second hour (11:00) is written - the already-covered first
    # hour (10:00) must not reappear.
    assert len(written_points) == 1
    assert written_points[0]["start"] == last_end
    # The running sum must continue from the prior known sum (10.0) plus
    # only the *new* hour's delta (1.0), not double-counting the
    # already-recorded first hour's delta on top of it.
    assert written_points[0]["sum"] == pytest.approx(11.0)


@pytest.mark.asyncio
async def test_build_day_statistics_aggregates_five_minute_intervals_into_hours():
    """Multiple 5-minute-interval deltas within the same hour are summed
    into a single hourly StatisticData point (HA's external statistics API
    only accepts top-of-the-hour timestamps)."""
    from custom_components.apsystems_cloud_crawler.statistics import _build_day_statistics

    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    times = [
        int((hour + timedelta(minutes=5 * i)).timestamp() * 1000) for i in range(12)
    ]
    batch = {
        "time": times,
        "dischargeEnergy": ["0.1"] * 12,
        "chargeEnergy": ["0.1"] * 12,
        "producedEnergy": ["0.1"] * 12,
        "consumedEnergy": ["0.1"] * 12,
        "exportedEnergy": ["0.1"] * 12,
        "importedEnergy": ["0.1"] * 12,
    }

    points, running_sum = _build_day_statistics(batch, "DE2", running_sum=5.0)

    assert len(points) == 1
    assert points[0]["start"] == hour
    assert points[0]["state"] == pytest.approx(1.2)  # 12 * 0.1
    assert points[0]["sum"] == pytest.approx(6.2)  # 5.0 + 1.2
    assert running_sum == pytest.approx(6.2)


@pytest.mark.asyncio
async def test_backfill_date_range_all_days_missing_returns_zero_for_all_keys():
    """If the portal never returns usable data for the whole range, every
    DE key reports zero days written and nothing is passed to
    async_add_external_statistics."""
    hass = FakeHass()
    client = AsyncMock()
    client.async_get_power_on_current_day_batch.return_value = None

    with patch(
        "custom_components.apsystems_cloud_crawler.statistics.get_last_statistics",
        return_value={},
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.async_add_external_statistics"
    ) as mock_add_stats, patch(
        "custom_components.apsystems_cloud_crawler.statistics.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        results = await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 1, 1),
            date(2026, 1, 2),
            pace_seconds=0,
        )

    assert all(days == 0 for days in results.values())
    mock_add_stats.assert_not_called()


def test_metadata_sets_mean_type_and_unit_class_for_energy_sum_statistics():
    """Regression test for a real HA deprecation warning: omitting
    ``mean_type``/``unit_class`` from StatisticMetaData logs "doesn't
    specify mean_type... will stop working in 2026.11". We only ever report
    a cumulative sum (no mean/min/max), so mean_type must be
    StatisticMeanType.NONE, and unit_class must be "energy" to match
    kWh."""
    from homeassistant.components.recorder.models import StatisticMeanType

    from custom_components.apsystems_cloud_crawler.statistics import _metadata

    for de_key in DAILY_ENERGY_SENSORS:
        meta = _metadata("entry123", "My Station", de_key)
        assert meta["mean_type"] == StatisticMeanType.NONE
        assert meta["unit_class"] == "energy"
        assert meta["has_mean"] is False
        assert meta["has_sum"] is True


@pytest.mark.asyncio
async def test_todays_first_hour_does_not_inherit_previous_days_total():
    """Regression test for a reported Energy dashboard data-quality bug: the
    very first hour of the currently-in-progress day appeared to contain an
    entire previous day's cumulative total (e.g. 19.021 kWh in a single
    5-minute-old bucket) instead of 0/near-0.

    Fixture: a complete previous day (288 5-minute intervals, calendar-
    aligned, with meaningful nonzero chargeEnergy deltas spread across it
    summing to 19.021) immediately followed by a brand new day that has
    only just started (3 intervals, all effectively zero chargeEnergy).
    Asserts the new day's first hour is written with a near-zero state and
    a sum that only reflects genuine carry-over of the running total, never
    the previous day's total value re-appearing as a single bucket's delta.
    """
    hass = FakeHass()
    client = AsyncMock()

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    base_yesterday = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    yesterday_time = [ms(base_yesterday + timedelta(minutes=5 * i)) for i in range(288)]
    charge_vals = ["0.0"] * 288
    charge_vals[100] = "9.5"
    charge_vals[150] = "9.521"
    assert sum(float(v) for v in charge_vals) == pytest.approx(19.021)
    yesterday_batch = {
        "time": yesterday_time,
        "chargeEnergy": charge_vals,
        "dischargeEnergy": ["0.04"] * 288,
        "producedEnergy": ["0.1"] * 288,
        "consumedEnergy": ["0.1"] * 288,
        "exportedEnergy": ["0.0"] * 288,
        "importedEnergy": ["0.0"] * 288,
    }

    base_today = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    today_time = [ms(base_today + timedelta(minutes=5 * i)) for i in range(3)]
    today_batch = {
        "time": today_time,
        "chargeEnergy": ["0.0", "0.0", "0.0"],
        "dischargeEnergy": ["0.07", "0.054", "0.061"],
        "producedEnergy": ["0.0", "0.0", "0.0"],
        "consumedEnergy": ["0.07", "0.054", "0.061"],
        "exportedEnergy": ["0.0", "0.0", "0.0"],
        "importedEnergy": ["0.0", "0.0", "0.0"],
    }

    client.async_get_power_on_current_day_batch.side_effect = [
        yesterday_batch,
        today_batch,
    ]

    written: dict[str, list] = {}

    def fake_add_external_statistics(hass_arg, metadata, points):
        written.setdefault(metadata["statistic_id"], []).extend(points)

    with patch(
        "custom_components.apsystems_cloud_crawler.statistics.get_last_statistics",
        return_value={},
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.async_add_external_statistics",
        side_effect=fake_add_external_statistics,
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 10, 6),
            date(2026, 10, 7),
            pace_seconds=0,
        )

    de1_id = _statistic_id("entry123", "DE1")
    today_midnight = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    todays_points = [p for p in written[de1_id] if p["start"] >= today_midnight]
    assert len(todays_points) == 1, "today's partial first hour should be a single point"
    assert todays_points[0]["state"] == pytest.approx(0.0)
    # The running sum must correctly carry the full previous day's total
    # forward (19.021), but as *sum*, never as today's bucket *state*.
    assert todays_points[0]["sum"] == pytest.approx(19.021)


@pytest.mark.asyncio
async def test_build_day_statistics_drops_implausible_interval_delta():
    """A single interval delta far beyond any plausible residential/C&I
    system's output (see MAX_PLAUSIBLE_INTERVAL_KWH) is dropped and logged
    rather than trusted, so a glitched/malformed portal response can't
    silently inject a huge spike into the Energy dashboard."""
    from custom_components.apsystems_cloud_crawler.statistics import _build_day_statistics

    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    times = [int((hour + timedelta(minutes=5 * i)).timestamp() * 1000) for i in range(3)]
    batch = {
        "time": times,
        "chargeEnergy": ["0.1", "999.0", "0.2"],  # 999.0 is implausible
        "dischargeEnergy": ["0.1"] * 3,
        "producedEnergy": ["0.1"] * 3,
        "consumedEnergy": ["0.1"] * 3,
        "exportedEnergy": ["0.1"] * 3,
        "importedEnergy": ["0.1"] * 3,
    }

    points, running_sum = _build_day_statistics(batch, "DE1", running_sum=0.0)

    assert len(points) == 1
    # Only the two plausible deltas (0.1 + 0.2) should be counted; the
    # implausible 999.0 must be dropped entirely.
    assert points[0]["state"] == pytest.approx(0.3)
    assert running_sum == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_force_backfills_older_range_after_newer_data_already_written():
    """Regression test for a real bug found while investigating the
    above-mentioned spike report: without ``force``, requesting a manual
    backfill for a date range entirely *older* than an already-known
    (more recent) statistic point silently writes zero points for every
    key - every hour in the requested range compares as "already covered"
    against the newer last-known point and is skipped, even though the
    user explicitly asked for that older data. ``force=True`` must make
    this range actually get written."""
    hass = FakeHass()
    client = AsyncMock()
    client.async_get_power_on_current_day_batch.return_value = _make_batch(1.0)

    # Simulate automatic backfill already having written a recent point
    # (e.g. from today), long after the older range being requested below.
    recent_end = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    def fake_get_last_statistics(hass_arg, count, stat_id, convert, types):
        return {stat_id: [{"end": recent_end.timestamp(), "sum": 50.0, "state": 1.0}]}

    with patch(
        "custom_components.apsystems_cloud_crawler.statistics.get_last_statistics",
        side_effect=fake_get_last_statistics,
    ), patch(
        "custom_components.apsystems_cloud_crawler.statistics.async_add_external_statistics"
    ) as mock_add_stats, patch(
        "custom_components.apsystems_cloud_crawler.statistics.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        # Without force: writes nothing (the bug).
        results_no_force = await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 1, 1),
            date(2026, 1, 1),
            pace_seconds=0,
            force=False,
        )
        assert all(days == 0 for days in results_no_force.values())
        mock_add_stats.assert_not_called()

        # With force: the older range is actually (re)written.
        results_force = await async_backfill_date_range(
            hass,
            "entry123",
            "My Station",
            client,
            date(2026, 1, 1),
            date(2026, 1, 1),
            pace_seconds=0,
            force=True,
        )
        assert all(days == 1 for days in results_force.values())
        assert mock_add_stats.call_count == len(DAILY_ENERGY_SENSORS)


def test_statistic_id_is_always_valid_per_ha_rule():
    """Regression test for a real HA bug: config entry ids are ULIDs (see
    homeassistant.util.ulid), rendered in UPPERCASE Crockford base32 and
    sometimes starting with a digit. HA's external statistic_id validation
    (recorder.statistics.valid_statistic_id) only allows lowercase
    letters/digits/underscore (no leading/trailing/double underscore) after
    the domain colon, so a raw, un-sanitized entry_id with uppercase
    letters previously produced an invalid id and
    `async_add_external_statistics` raised
    `HomeAssistantError("Invalid statistic_id")` for every real-world
    install. Covers a realistic uppercase ULID-shaped entry_id (which can
    start with a digit) to prevent regression."""
    entry_ids = [
        "2c9f95c795926c87019594129f31635",  # digit-leading hex (also valid)
        "01M49GGB055ZF0QF0827N1PPE5",  # realistic uppercase ULID, digit-leading
        "01JABCXYZDEF0123456789ABCD",  # another uppercase ULID shape
    ]
    for entry_id in entry_ids:
        for de_key in DAILY_ENERGY_SENSORS:
            statistic_id = _statistic_id(entry_id, de_key)
            assert valid_statistic_id(statistic_id), (
                f"generated statistic_id {statistic_id!r} for entry_id "
                f"{entry_id!r} is invalid per HA's own validator"
            )

