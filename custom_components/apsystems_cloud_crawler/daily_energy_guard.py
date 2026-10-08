"""Guards the six daily energy counters (DE0-DE5) against spurious resets.

Two related problems can corrupt the Energy dashboard if these "daily"
counters are passed straight through from the portal to Home Assistant's
plain ``total_increasing`` sensors (see sensor.py's ``DAILY_ENERGY_DESCRIPTIONS``):

1. **Transient zero/low glitches.** The portal has been observed to
   occasionally report an implausibly low value for one of these counters
   on a single poll, immediately followed by a poll that resumes at (or
   above) the pre-dip value - not a genuine midnight reset, just a backend
   hiccup. A plain ``total_increasing`` sensor has no way to tell this
   apart from a real reset: HA's recorder records the dip as "meter reset
   to 0", then records the very next, much higher reading as new
   post-reset growth, effectively re-adding almost an entire day's energy
   to the Energy dashboard's running total (a severe, silent skew).
2. **Day-boundary misalignment.** The portal's own day boundary is not
   guaranteed to land on this Home Assistant instance's local midnight
   (e.g. the portal resets on a UTC day boundary while HA runs in CET) -
   so "local calendar date changed" is the wrong signal to use for
   confirming a genuine reset.

``DailyEnergyRolloverGuard`` addresses both by never trusting a decrease
on its own. Any drop in a counter is held back (the previously published,
higher value keeps being reported) until it can be corroborated using an
independent signal: the portal's own current-day interval series
(``getSystemPowerOnCurrentDayBatch``). A day that has genuinely just
started has few (or zero) 5-minute-interval entries so far; a day that is
well underway (the expected state during a transient glitch) has many.
This corroboration is keyed off the portal's own self-reported
``lastReportTime`` date (not Home Assistant's local date), so the decision
is correct regardless of any timezone offset between the portal and this
HA instance - problem 2 above is sidestepped entirely by never relying on
a local-clock-derived "today" in the first place.

This holding behaviour is exactly what keeps the Energy dashboard from
using "leftovers" of the previous day during the first poll(s) after a
real rollover: a confirmed rollover switches every counter straight to its
fresh post-reset value in one step, rather than the dashboard ever seeing
an in-between, ambiguous state.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

from .api import ApsystemsCloudCrawlerError
from .const import (
    DAILY_ROLLOVER_EPSILON_KWH,
    DAILY_ROLLOVER_FORCE_ACCEPT_AFTER,
    DAILY_ROLLOVER_MAX_CONFIRM_POINTS,
    DAILY_ROLLOVER_RECHECK_INTERVAL,
)

if TYPE_CHECKING:
    from .api import ApsystemsCloudCrawlerClient

_LOGGER = logging.getLogger(__name__)


def _day_string_from_report_time(raw_last_report_time: str | None) -> str | None:
    """Extract a ``yyyyMMdd`` day string from the portal's own ``lastReportTime``.

    ``raw_last_report_time`` is the raw ``"yyyy-MM-dd HH:mm:ss"`` string as
    returned by the portal (see models.parse_control_info) - we deliberately
    read the date portion directly from it rather than reinterpreting it
    into any particular timezone: whatever timezone the portal's clock
    actually runs on, this string's date portion is the portal's own
    opinion of "what day is it", which is exactly the label
    ``getSystemPowerOnCurrentDayBatch`` expects for "today". Falls back to
    ``None`` (caller substitutes its own guess) if missing/unparseable.
    """
    if not raw_last_report_time or len(raw_last_report_time) < 10:
        return None
    date_part = raw_last_report_time[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return None
    return date_part.replace("-", "")


class DailyEnergyRolloverGuard:
    """Stateful, per-coordinator filter for the DE0-DE5 daily counters.

    One instance should be kept for the lifetime of a config entry (see
    coordinator.py) since the filtering depends on values observed on
    previous polls.
    """

    def __init__(self) -> None:
        self._last_raw: dict[str, float] = {}
        self._last_published: dict[str, float] = {}
        self._pending_since: datetime | None = None
        self._last_confirm_attempt: datetime | None = None

    async def async_filter(
        self,
        client: ApsystemsCloudCrawlerClient,
        raw_values: dict[str, float | None],
        raw_last_report_time: str | None,
        now: datetime,
    ) -> dict[str, float | None]:
        """Return the values that should actually be published this cycle.

        ``raw_values`` maps DE key (``DE0``..``DE5``) to the freshly-parsed
        float value for this poll (or ``None`` if unavailable/unparseable).
        """
        decreased_keys = [
            key
            for key, value in raw_values.items()
            if value is not None
            and (prev := self._last_raw.get(key)) is not None
            and value < prev - DAILY_ROLLOVER_EPSILON_KWH
        ]

        if not decreased_keys:
            if self._pending_since is not None:
                _LOGGER.info(
                    "Daily energy counter dip observed at %s resolved without "
                    "a confirmed rollover (values recovered); treating it as a "
                    "transient portal glitch and discarding it",
                    self._pending_since.isoformat(),
                )
                self._pending_since = None
                self._last_confirm_attempt = None
            self._advance(raw_values.keys(), raw_values)
            return dict(self._last_published)

        if self._pending_since is None:
            self._pending_since = now

        verdict = await self._async_maybe_confirm(client, raw_last_report_time, now)

        if verdict is None and now - self._pending_since >= DAILY_ROLLOVER_FORCE_ACCEPT_AFTER:
            _LOGGER.warning(
                "Could not corroborate a drop in daily energy counter(s) %s "
                "after %s; forcing acceptance as a genuine rollover to avoid "
                "the sensor(s) getting stuck on a stale value",
                sorted(decreased_keys),
                DAILY_ROLLOVER_FORCE_ACCEPT_AFTER,
            )
            verdict = True

        if verdict is True:
            _LOGGER.debug(
                "Confirmed daily energy counter rollover for %s", sorted(decreased_keys)
            )
            for key, value in raw_values.items():
                if value is not None:
                    self._last_raw[key] = value
                    self._last_published[key] = value
            self._pending_since = None
            self._last_confirm_attempt = None
        elif verdict is False:
            _LOGGER.info(
                "Discarding transient dip in daily energy counter(s) %s "
                "(not corroborated by a new day's interval count)",
                sorted(decreased_keys),
            )
            self._advance(
                raw_values.keys() - set(decreased_keys), raw_values
            )
            self._pending_since = None
            self._last_confirm_attempt = None
        else:
            # Inconclusive and not yet past the force-accept timeout: hold
            # the decreased keys at their last published value, but let
            # every other key keep progressing normally.
            self._advance(raw_values.keys() - set(decreased_keys), raw_values)

        return dict(self._last_published)

    def _advance(self, keys: Iterable[str], raw_values: dict[str, float | None]) -> None:
        """Advance raw/published tracking for ``keys`` to their new raw value."""
        for key in keys:
            value = raw_values.get(key)
            if value is None:
                continue
            self._last_raw[key] = value
            self._last_published[key] = max(self._last_published.get(key, value), value)

    async def _async_maybe_confirm(
        self,
        client: ApsystemsCloudCrawlerClient,
        raw_last_report_time: str | None,
        now: datetime,
    ) -> bool | None:
        """Attempt corroboration, respecting the recheck backoff.

        Returns ``True`` (confirmed genuine rollover), ``False`` (confirmed
        glitch) or ``None`` (inconclusive / not attempted this cycle).
        """
        if (
            self._last_confirm_attempt is not None
            and now - self._last_confirm_attempt < DAILY_ROLLOVER_RECHECK_INTERVAL
        ):
            return None

        self._last_confirm_attempt = now
        # Fallback only applies if the portal's own lastReportTime was
        # missing/unparseable this cycle; using `now` (UTC, as passed by the
        # coordinator) here is a last resort; it is not the primary
        # TZ-safe mechanism described in the module docstring.
        day_str = _day_string_from_report_time(raw_last_report_time) or now.strftime(
            "%Y%m%d"
        )
        try:
            batch = await client.async_get_power_on_current_day_batch(day_str)
        except ApsystemsCloudCrawlerError:
            _LOGGER.debug(
                "Could not corroborate potential daily counter rollover for %s",
                day_str,
                exc_info=True,
            )
            return None

        if not batch:
            return None

        times = batch.get("time") or []
        confirmed = len(times) <= DAILY_ROLLOVER_MAX_CONFIRM_POINTS
        _LOGGER.debug(
            "Rollover corroboration for %s: %d interval point(s) -> %s",
            day_str,
            len(times),
            "confirmed rollover" if confirmed else "glitch",
        )
        return confirmed
