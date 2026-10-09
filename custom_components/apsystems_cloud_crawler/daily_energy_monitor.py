"""Diagnostic logging for the six daily energy counters (DE0-DE5).

This integration's native sensors for these counters are plain
``total_increasing`` entities (see sensor.py's ``DAILY_ENERGY_DESCRIPTIONS``).
That state class is *designed* to handle a counter decreasing: Home
Assistant's recorder treats a lower reading as the start of a fresh
accumulation baseline, which is exactly the behaviour a genuine portal day
rollover (flush to 0, or near it) needs - no special handling required.

What is worth watching for is a decrease that *doesn't* look like a clean
rollover, or any other internally-inconsistent reading (e.g. a counter
that should be monotonic within a day going backwards without flushing
to zero). Two distinct things can cause exactly this kind of confusing
decrease:

* The portal's day boundary is not guaranteed to land on this Home
  Assistant instance's local midnight (e.g. UTC rollover vs. a CET
  install), so "local date changed" is not a reliable signal to reason
  about when a rollover *should* happen.
* The portal has occasionally been observed to report a transient,
  implausibly low value for one of these counters on a single poll,
  before resuming at its previous level on the very next poll.

``DailyEnergyRolloverMonitor`` never withholds or rewrites a value - it
only logs, with enough context to diagnose what actually happened: the
live power readings for that poll, all six counters' previous/new values,
the portal's own ``lastReportTime``, and (best-effort) how many interval
points the portal's current-day series has so far, which tells apart a
day that has genuinely just started from one that is already well
underway.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .api import ApsystemsCloudCrawlerError
from .const import (
    DAILY_ROLLOVER_EPSILON_KWH,
    DAILY_ROLLOVER_MAX_CONFIRM_POINTS,
    DAILY_ROLLOVER_NEAR_ZERO_KWH,
    DAILY_ROLLOVER_RECHECK_INTERVAL,
)
from .portal_day import day_string_from_report_time

if TYPE_CHECKING:
    from .api import ApsystemsCloudCrawlerClient

_LOGGER = logging.getLogger(__name__)



class DailyEnergyRolloverMonitor:
    """Stateful, per-coordinator diagnostic watcher for the DE0-DE5 counters.

    Never alters what gets published; only logs. One instance should be
    kept for the lifetime of a config entry (see coordinator.py) since
    detecting a decrease requires remembering the previous poll's values.
    """

    def __init__(self) -> None:
        self._last_raw: dict[str, float] = {}
        self._last_confirm_attempt: datetime | None = None

    async def async_observe(
        self,
        client: ApsystemsCloudCrawlerClient,
        raw_values: dict[str, float | None],
        raw_last_report_time: str | None,
        live_context: dict[str, Any],
        now: datetime,
    ) -> None:
        """Log diagnostics for this poll's daily counters; never raises.

        ``raw_values`` maps DE key (``DE0``..``DE5``) to this poll's
        freshly-parsed float value (or ``None``). ``live_context`` is a
        dict of additional current readings (live power values etc.) to
        include verbatim in any diagnostic log line.
        """
        decreases = {
            key: (self._last_raw[key], value)
            for key, value in raw_values.items()
            if value is not None
            and (prev := self._last_raw.get(key)) is not None
            and value < prev - DAILY_ROLLOVER_EPSILON_KWH
        }

        if decreases:
            await self._async_log_decrease(
                client, decreases, raw_last_report_time, live_context, now
            )

        for key, value in raw_values.items():
            if value is not None:
                self._last_raw[key] = value

    async def _async_log_decrease(
        self,
        client: ApsystemsCloudCrawlerClient,
        decreases: dict[str, tuple[float, float]],
        raw_last_report_time: str | None,
        live_context: dict[str, Any],
        now: datetime,
    ) -> None:
        all_near_zero = all(
            new <= DAILY_ROLLOVER_NEAR_ZERO_KWH for _prev, new in decreases.values()
        )
        corroboration = await self._async_maybe_corroborate(
            client, raw_last_report_time, now
        )

        dump = {
            "last_report_time": raw_last_report_time,
            "decreased_counters": {
                key: {"previous": prev, "new": new} for key, (prev, new) in decreases.items()
            },
            "live": live_context,
            "current_day_interval_points": corroboration,
        }

        if all_near_zero:
            _LOGGER.warning(
                "Daily energy counter(s) %s flushed to (near) zero - looks "
                "like a genuine portal day rollover (Home Assistant's "
                "total_increasing handling will treat this as a new "
                "accumulation period, which is expected). Diagnostic dump: %s",
                sorted(decreases),
                dump,
            )
        else:
            _LOGGER.warning(
                "Daily energy counter(s) %s decreased WITHOUT flushing to "
                "(near) zero - this does not look like a normal day "
                "rollover and may indicate a portal data inconsistency. "
                "Diagnostic dump: %s",
                sorted(decreases),
                dump,
            )

    async def _async_maybe_corroborate(
        self,
        client: ApsystemsCloudCrawlerClient,
        raw_last_report_time: str | None,
        now: datetime,
    ) -> int | str | None:
        """Best-effort: how many current-day interval points the portal has.

        Purely diagnostic annotation for the log line - never affects what
        is published. Returns the point count, or a short string
        explaining why it is unavailable (rate-limited/unusable response/
        error), or ``None`` if the call itself failed.
        """
        if (
            self._last_confirm_attempt is not None
            and now - self._last_confirm_attempt < DAILY_ROLLOVER_RECHECK_INTERVAL
        ):
            return "skipped (rechecked recently)"

        self._last_confirm_attempt = now
        day_str = day_string_from_report_time(raw_last_report_time) or now.strftime(
            "%Y%m%d"
        )
        try:
            batch = await client.async_get_power_on_current_day_batch(day_str)
        except ApsystemsCloudCrawlerError:
            _LOGGER.debug(
                "Could not fetch current-day interval data for %s to annotate "
                "the diagnostic dump",
                day_str,
                exc_info=True,
            )
            return "unavailable (request failed)"

        if not batch:
            return "unavailable (no usable data for this day)"

        times = batch.get("time") or []
        _LOGGER.debug(
            "Current-day interval count for %s: %d point(s) (<=%d would be "
            "consistent with a just-started day)",
            day_str,
            len(times),
            DAILY_ROLLOVER_MAX_CONFIRM_POINTS,
        )
        return len(times)
