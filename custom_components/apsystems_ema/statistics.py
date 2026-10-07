"""External-statistics backfill for the six daily energy counters.

Design notes (read before touching this file):

* The native ``total_increasing`` sensor entities created in ``sensor.py``
  are the primary, user-facing Energy-dashboard source. Users add them to
  the Energy dashboard themselves; this integration never touches Energy
  dashboard preferences/config entries.
* The external statistics written here are a *separate*, clearly
  integration-owned backing store (statistic_id
  ``apsystems_ema:entry_<sanitized_entry_id>_<de_key>``) used only to
  backfill historical hourly-resolution data (HA's external statistics API
  only accepts hour-aligned timestamps; see ``_build_day_statistics``)
  after a fresh install or HA downtime. They are
  intentionally namespaced with a ``domain:id`` form (external statistic
  id) so they can never collide with a native sensor's own statistic_id,
  which HA always derives from the entity_id (e.g. ``sensor.xxx``) for
  internal, non-external statistics. This code only ever reads/writes
  statistic_ids it generates itself (see ``_statistic_id``), so it cannot
  overwrite another integration's or the user's own statistics.
* Config entry ids are ULIDs (see ``homeassistant.util.ulid``), which are
  rendered in UPPERCASE Crockford base32 and can start with a digit. HA's
  external statistic_id validation (``recorder.statistics.valid_statistic_id``)
  requires the part after the domain colon to match ``[\\da-z_]+`` with no
  leading/trailing/double underscore - i.e. lowercase only. A raw
  ``entry_id`` therefore fails validation whenever it contains an uppercase
  letter (which is the common case), raising
  ``HomeAssistantError("Invalid statistic_id")`` from
  ``async_add_external_statistics``. ``_sanitize_entry_id`` makes the
  generated id valid *by construction*, regardless of entry_id's exact
  format, rather than relying on it happening to already be lowercase.
* ``async_backfill_statistics`` (automatic, runs on every config entry
  setup) and ``async_backfill_date_range`` (manual, triggered by the
  ``apsystems_ema.backfill`` service for a user-chosen date range) share the
  same per-day core, ``_async_backfill_dates``, which fetches
  getSystemPowerOnCurrentDayBatch exactly ONCE per day (its response
  already contains the per-interval series for all six counters) and
  distributes the per-interval-delta-to-cumulative-sum conversion to every
  DE key from that single response, rather than re-fetching the same day
  once per counter.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.core import HomeAssistant

from .api import ApsystemsEmaClient
from .const import (
    BACKFILL_MAX_DAYS,
    DAILY_ENERGY_BATCH_FIELDS,
    DAILY_ENERGY_SENSORS,
    DOMAIN,
    MAX_PLAUSIBLE_INTERVAL_KWH,
)

_LOGGER = logging.getLogger(__name__)

# HA's external statistic_id validation only allows lowercase
# letters/digits/underscore after the domain colon (see module docstring).
_INVALID_STATISTIC_ID_CHARS = re.compile(r"[^a-z0-9_]+")
_REPEATED_UNDERSCORES = re.compile(r"_+")


def _sanitize_entry_id(entry_id: str) -> str:
    """Make a config entry_id safe to embed in an external statistic_id.

    Entry ids are ULIDs and may contain uppercase letters (invalid here) or
    start with a digit (actually fine per HA's regex, but we don't rely on
    that). Lowercase it and replace any disallowed character with an
    underscore, collapsing repeats and trimming leading/trailing
    underscores so the result can never produce an invalid "__" sequence or
    a leading/trailing underscore once combined with the surrounding
    "entry_"/"_<suffix>" parts.
    """
    sanitized = _INVALID_STATISTIC_ID_CHARS.sub("_", entry_id.lower())
    sanitized = _REPEATED_UNDERSCORES.sub("_", sanitized).strip("_")
    return sanitized


def _statistic_id(entry_id: str, de_key: str) -> str:
    suffix = DAILY_ENERGY_SENSORS[de_key]["key"]
    return f"{DOMAIN}:entry_{_sanitize_entry_id(entry_id)}_{suffix}"


def _metadata(entry_id: str, title: str, de_key: str) -> StatisticMetaData:
    meta = DAILY_ENERGY_SENSORS[de_key]
    return StatisticMetaData(
        has_mean=False,
        # We only ever report a cumulative `sum` (no mean/min/max), so
        # mean_type is NONE. HA logs a deprecation warning ("doesn't specify
        # mean_type... will stop working in 2026.11") if this is omitted.
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        # "(History)" suffix is deliberate: this statistic_id is a distinct
        # entry from the native `sensor.xxx` entity of the same metric in
        # HA's Energy-dashboard source picker (both are valid, selectable,
        # unit_class=energy "sum" statistics - the picker has no way to
        # know they're related). Without a visibly different name the two
        # are easy to confuse; a user who adds the *sensor* as their Energy
        # source will see no backfilled history (HA's public recorder API
        # has no way for a custom integration to write external history
        # into an entity's own statistic_id - only this separate,
        # explicitly-namespaced one). See README's "Energy dashboard"
        # section for which one to pick and why.
        name=f"{title} {meta['name']} (History)",
        source=DOMAIN,
        statistic_id=_statistic_id(entry_id, de_key),
        unit_of_measurement="kWh",
        # Same deprecation class as mean_type (unit_class is also checked by
        # HA's `report_usage` and will become required in 2026.11); "energy"
        # matches UnitOfEnergy.KILO_WATT_HOUR's unit class.
        unit_class="energy",
    )


async def _async_get_last_stat(
    hass: HomeAssistant, statistic_id: str
) -> tuple[datetime, float] | None:
    """Return the (end time, cumulative sum) of the most recent known statistic point."""
    result = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum", "state"}
    )
    stats = result.get(statistic_id)
    if not stats:
        return None
    end_time = datetime.fromtimestamp(stats[0]["end"], tz=UTC)
    return end_time, float(stats[0]["sum"])


def _build_day_statistics(
    batch: dict[str, Any],
    de_key: str,
    running_sum: float,
    last_time: datetime | None = None,
) -> tuple[list[StatisticData], float]:
    """Build hourly StatisticData points for one day/one DE counter.

    ``last_time``, when given, is the start of the earliest hour not yet
    covered by a previously-written statistic point (see
    ``_async_backfill_dates``). Hours strictly before it are skipped
    *before* being folded into ``running_sum`` - not summed and then
    discarded - so that resuming mid-day (e.g. HA restarted partway
    through today) never double-counts already-recorded hours into the
    cumulative total carried forward to later points/days.

    ``series`` entries in getSystemPowerOnCurrentDayBatch are per-5-minute-
    interval deltas (confirmed by summing a day's series and comparing
    against the matching ``*Total`` scalar for that day - they match within
    rounding). HA's *external* statistics API only accepts hourly-aligned
    timestamps (``start.minute == 0 and start.second == 0``, checked by
    ``recorder.statistics.async_import_statistics``) - unlike HA's internal
    short-term-statistics table, external statistics have no 5-minute
    resolution; passing raw 5-minute-interval points straight through
    raises ``HomeAssistantError("Invalid timestamp: ... top of the
    hour...")``.

    So the per-5-minute deltas are summed into whole-hour buckets (up to 12
    intervals per hour) before building one ``StatisticData`` point per
    hour. Bucket boundaries are anchored to round *UTC* hours directly from
    the absolute epoch-millisecond timestamps the API returns - unlike the
    naive, timezone-less ``lastReportTime`` wall-clock string elsewhere in
    this integration, these ``time`` values are genuine Unix timestamps
    (unambiguous instants), so there is no "which timezone is this naive
    string in" question to resolve here; UTC-hour buckets are simply
    always-valid, DST-safe boundaries, and Home Assistant renders the
    resulting hourly bars in the user's local timezone automatically, so
    this does not change what hour a user sees energy attributed to.

    The last (partial) hour of "today" naturally still gets a point - if
    the API truncates today's series to "now", that hour's bucket just has
    fewer than 12 intervals summed into it, which is valid partial-hour
    data as long as its timestamp is still the top of the hour.

    The running cumulative ``sum`` is still accumulated per-delta (now
    within each hour bucket) exactly as before - only the *grouping* of
    deltas into statistic points changed, not the per-interval-delta
    parsing/summing logic itself.
    """
    fields = DAILY_ENERGY_BATCH_FIELDS[de_key]
    series = batch.get(fields["series"])
    times = batch.get("time")
    if not series or not times or len(series) != len(times):
        return [], running_sum

    # dict preserves insertion order; the API already returns intervals in
    # chronological order, so hour buckets are naturally encountered in
    # order too (sorted() below is just a defensive guarantee, not load
    # bearing for correctness).
    hourly_deltas: dict[datetime, float] = {}
    for ts_millis, value_str in zip(times, series, strict=False):
        try:
            delta = float(value_str)
        except (TypeError, ValueError):
            continue
        # Defense-in-depth: reject any single 5-minute interval that is
        # wildly implausible for the small systems this integration targets
        # (see MAX_PLAUSIBLE_INTERVAL_KWH's docstring in const.py). This
        # guards against a malformed/glitched portal response silently
        # corrupting the Energy dashboard with a huge spike in a single
        # bucket - we have observed the portal's own data be clean in
        # testing, but this costs nothing and catches it if it ever isn't.
        if abs(delta) > MAX_PLAUSIBLE_INTERVAL_KWH:
            instant = datetime.fromtimestamp(ts_millis / 1000, tz=UTC)
            _LOGGER.warning(
                "Dropping implausible %s interval delta %.3f kWh at %s "
                "(exceeds %.1f kWh/5min sanity bound) - treating as 0 to "
                "avoid corrupting the Energy dashboard with a spike",
                de_key,
                delta,
                instant.isoformat(),
                MAX_PLAUSIBLE_INTERVAL_KWH,
            )
            continue
        instant = datetime.fromtimestamp(ts_millis / 1000, tz=UTC)
        hour_start = instant.replace(minute=0, second=0, microsecond=0)
        hourly_deltas[hour_start] = hourly_deltas.get(hour_start, 0.0) + delta

    points: list[StatisticData] = []
    for hour_start in sorted(hourly_deltas):
        if last_time is not None and hour_start < last_time:
            continue
        hour_delta = hourly_deltas[hour_start]
        running_sum += hour_delta
        points.append(StatisticData(start=hour_start, sum=running_sum, state=hour_delta))
    return points, running_sum


async def _async_backfill_dates(
    hass: HomeAssistant,
    entry_id: str,
    title: str,
    client: ApsystemsEmaClient,
    start_date: date,
    end_date: date,
    last_known_by_key: dict[str, tuple[datetime, float] | None],
    *,
    pace_seconds: float | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Walk ``start_date``..``end_date`` (inclusive) writing external statistics for all
    six DE keys.

    Fetches getSystemPowerOnCurrentDayBatch exactly once per day (its
    response already contains every counter's per-interval series) and
    builds/writes points for every DE key from that single response, rather
    than hitting the portal once per key per day. A day with no usable
    response (e.g. outside the portal's retention window, or simply an
    invalid date) is logged as a warning and skipped, never aborting the
    rest of the range. Returns ``{de_key: days_written}``.

    ``force`` (only ever passed ``True`` from the manual
    ``apsystems_ema.backfill`` service, never from the automatic startup
    path) disables the normal "skip hours already covered by the last known
    statistic point" behaviour on a per-key basis, for any key whose last
    known point is NOT strictly before ``start_date``. Without this, two
    real problems exist for a manually-requested range:

    1. Requesting a range that is entirely *before* a key's last known
       statistic point (e.g. backfilling older history discovered after
       automatic backfill already wrote more recent days) silently writes
       *zero* points for that key - every hour in the requested range
       compares as "before the last known point" and is skipped, even
       though the user explicitly asked for exactly that data.
    2. Re-running backfill for a range that *overlaps* already-written
       points (e.g. "fix today, its data looked wrong") is a no-op for the
       same reason, so there is no way to self-heal a statistic point that
       was written incorrectly by an older, since-fixed version of this
       integration's backfill logic (external statistics, once written,
       are never retroactively corrected by a code fix alone).

    When ``force`` triggers for a key (last known point's date >=
    ``start_date``, i.e. the request reaches into already-written
    territory), that key's running sum and skip-cutoff are reset to a fresh
    baseline (0.0 / no cutoff) for the *entire* requested range, and every
    requested day is fully (re)written from the portal's current data. This
    guarantees correct per-hour `state` (delta) values throughout the
    forced range, at the cost of a one-time `sum` discontinuity at the
    boundary between the forced range and any older, untouched data for
    that key from before ``start_date`` (HA's Energy dashboard bars reflect
    `state`, not `sum`, so this discontinuity does not distort the
    dashboard's period totals, only a cumulative "lifetime total" view
    spanning across that exact boundary). Keys whose last known point is
    strictly before ``start_date`` (a pure, non-overlapping forward
    extension) are left on their normal continuation path, since there is
    nothing to reconcile.
    """
    running_sums = {k: (v[1] if v else 0.0) for k, v in last_known_by_key.items()}
    # last_times tracks, per key, the *end* of the most recently known/
    # written statistic point (i.e. the start of the next expected point),
    # consistently - get_last_statistics() returns "end" = start + 1 hour
    # for an hourly external statistic, so a freshly-fetched point is new
    # iff its start is >= that end (not strictly > - the previous "> "
    # comparison here incorrectly treated the immediately-next point as
    # already covered, silently dropping one point - previously one
    # 5-minute interval, now a whole hour - every time backfill resumed).
    last_times = {k: (v[0] if v else None) for k, v in last_known_by_key.items()}
    if force:
        range_start = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        for de_key, last_known in last_known_by_key.items():
            if last_known is not None and last_known[0] >= range_start:
                running_sums[de_key] = 0.0
                last_times[de_key] = None
    metadata = {k: _metadata(entry_id, title, k) for k in DAILY_ENERGY_SENSORS}
    days_written = dict.fromkeys(DAILY_ENERGY_SENSORS, 0)

    day = start_date
    while day <= end_date:
        day_str = day.strftime("%Y%m%d")
        batch = await client.async_get_power_on_current_day_batch(day_str)
        if not batch or not batch.get("time"):
            _LOGGER.warning(
                "No usable data for %s (likely outside the portal's retention "
                "window or an invalid date); skipping this day for all counters",
                day_str,
            )
        else:
            for de_key in DAILY_ENERGY_SENSORS:
                last_time = last_times[de_key]
                points, running_sums[de_key] = _build_day_statistics(
                    batch, de_key, running_sums[de_key], last_time
                )
                if points:
                    async_add_external_statistics(hass, metadata[de_key], points)
                    last_times[de_key] = points[-1]["start"] + timedelta(hours=1)
                    days_written[de_key] += 1

        day += timedelta(days=1)
        if pace_seconds and day <= end_date:
            await asyncio.sleep(pace_seconds)

    return days_written


async def async_backfill_statistics(
    hass: HomeAssistant, entry_id: str, title: str, client: ApsystemsEmaClient
) -> None:
    """Backfill external statistics for the six daily energy counters.

    Walks backward from today for up to ``BACKFILL_MAX_DAYS`` days (or until
    the portal returns no usable data for a day, whichever is sooner),
    stopping early for any day already covered by existing statistics. Runs
    automatically on every config entry setup; see
    ``async_backfill_date_range`` for the user-invokable equivalent over an
    arbitrary date range (the ``apsystems_ema.backfill`` service).
    """
    today = datetime.now(UTC).date()
    last_known_by_key: dict[str, tuple[datetime, float] | None] = {}
    earliest_bound = today - timedelta(days=BACKFILL_MAX_DAYS - 1)
    start_date = today
    for de_key in DAILY_ENERGY_SENSORS:
        statistic_id = _statistic_id(entry_id, de_key)
        last_stat = await _async_get_last_stat(hass, statistic_id)
        last_known_by_key[de_key] = last_stat
        key_start = earliest_bound
        if last_stat is not None:
            key_start = max(earliest_bound, last_stat[0].date())
        start_date = min(start_date, key_start)

    days_written = await _async_backfill_dates(
        hass, entry_id, title, client, start_date, today, last_known_by_key
    )
    for de_key, days in days_written.items():
        if days:
            _LOGGER.debug(
                "Automatic startup backfill wrote %d day(s) for %s",
                days,
                _statistic_id(entry_id, de_key),
            )


async def async_backfill_date_range(
    hass: HomeAssistant,
    entry_id: str,
    title: str,
    client: ApsystemsEmaClient,
    start_date: date,
    end_date: date,
    *,
    pace_seconds: float = 1.0,
    force: bool = False,
) -> dict[str, int]:
    """Backfill external statistics for all six daily energy counters over a user-chosen range.

    This is the implementation behind the ``apsystems_ema.backfill`` service,
    for manually filling history (e.g. right after first install, to cover
    time before the integration was ever running, or any time a gap is
    suspected) without waiting for the automatic startup gap-fill or
    restarting Home Assistant.

    Unlike the automatic backfill, this does not bound the range by
    ``BACKFILL_MAX_DAYS`` (the service schema/handler is responsible for its
    own sanity cap), but does pace requests with ``pace_seconds`` between
    each per-day portal request so a long manually-requested range doesn't
    hammer the site. A day with no usable response is logged as a warning
    and skipped; it never aborts the rest of the range.

    ``force`` lets the user re-write a range that reaches into
    already-covered territory - either to fill in older history discovered
    after newer days were already backfilled (normally a silent no-op, see
    ``_async_backfill_dates``'s docstring), or to self-heal a statistic
    point suspected to have been written incorrectly by an older version of
    this integration. See ``_async_backfill_dates`` for the exact semantics
    and the one-time ``sum`` discontinuity this trades off.

    Returns a dict of ``{de_key: days_written}`` for status reporting.
    """
    last_known_by_key: dict[str, tuple[datetime, float] | None] = {}
    for de_key in DAILY_ENERGY_SENSORS:
        last_known_by_key[de_key] = await _async_get_last_stat(
            hass, _statistic_id(entry_id, de_key)
        )

    days_written = await _async_backfill_dates(
        hass,
        entry_id,
        title,
        client,
        start_date,
        end_date,
        last_known_by_key,
        pace_seconds=pace_seconds,
        force=force,
    )
    for de_key, days in days_written.items():
        _LOGGER.info(
            "Manual backfill for %s (%s to %s): wrote %d day(s)",
            _statistic_id(entry_id, de_key),
            start_date,
            end_date,
            days,
        )
    return days_written
