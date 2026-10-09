"""Synthesized, never-resetting lifetime counters for the daily energy sensors.

The portal only reports a genuine lifetime (never-resetting) total for two
of the six daily counters: solar production and local consumption
(``pvLifetimeEnergy``/``consumeLifetimeEnergy``, exposed directly as
``pv_lifetime_energy``/``consume_lifetime_energy`` - see sensor.py). The
other four (battery charge/discharge, grid import/export) only exist as
daily-resetting counters.

Feeding the Energy dashboard a daily-resetting counter is workable (Home
Assistant's ``total_increasing`` state class is designed to handle the
reset), but it is sensitive to exactly *when* HA samples the counter
relative to the portal's own rollover instant: if the portal's day
boundary doesn't land on HA's local midnight, or simply hasn't been
polled yet at the moment some other mechanism (e.g. the Energy
dashboard's own day bucketing, or a Utility Meter helper) snapshots
"today's starting value", that snapshot can end up being yesterday's
final total instead of the new day's near-zero baseline - which is
exactly the kind of mismatch that can make a day's figures look doubled.

A sensor that never resets sidesteps this class of problem entirely:
Home Assistant just differences two cumulative readings, so the precise
instant of a portal-side reset stops mattering. This module reconstructs
such a sensor for the four counters that don't already have one, by
accumulating each poll's positive delta into a persisted running total.
The only wrinkle is telling a genuine day rollover (counter drops because
a new accumulation period started) apart from a transient portal glitch
(counter drops for one poll, then recovers) - this reuses the same
portal-self-reported-date signal as daily_energy_monitor.py, which is
free (no extra network call) and immune to any HA/portal timezone
mismatch: if the portal's own ``lastReportTime`` date advanced between
two polls, the drop is a real rollover; if it didn't, it is held as
suspicious until it either recovers or a bounded timeout forces
acceptance (see ``LIFETIME_SUSPICIOUS_HOLD_TIMEOUT``).

Crucially, this is purely additive and fully independent from the
existing daily sensors: it never reads back or mutates them, so it cannot
introduce any regression there even if its own heuristic is wrong on an
edge case.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DAILY_ROLLOVER_EPSILON_KWH,
    DOMAIN,
    LIFETIME_ENERGY_SENSORS,
    LIFETIME_SUSPICIOUS_HOLD_TIMEOUT,
    MANUFACTURER,
    MODEL,
)
from .coordinator import ApsystemsCloudCrawlerCoordinator
from .portal_day import day_string_from_report_time

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TrackerState:
    """Immutable snapshot of one lifetime counter's tracking state."""

    accumulated: float = 0.0
    last_raw: float | None = None
    last_portal_day: str | None = None
    held_since: datetime | None = None


def advance_tracker(
    state: _TrackerState, raw: float | None, day_string: str | None, now: datetime
) -> tuple[_TrackerState, str]:
    """Compute the next tracker state for one new poll's raw daily-counter reading.

    Pure function (no I/O, no logging) so it's trivial to unit test; returns
    the new state plus a short status tag describing what happened, for the
    caller to log/act on as it sees fit.
    """
    if raw is None:
        return state, "no_data"

    if state.last_raw is None:
        # First observation since (re)start: nothing to compare against yet,
        # so just establish the baseline. Any previously-accumulated total
        # (restored from a prior run) is left untouched.
        return (
            replace(state, last_raw=raw, last_portal_day=day_string, held_since=None),
            "baseline",
        )

    if raw >= state.last_raw - DAILY_ROLLOVER_EPSILON_KWH:
        delta = max(raw - state.last_raw, 0.0)
        return (
            replace(
                state,
                accumulated=state.accumulated + delta,
                last_raw=raw,
                last_portal_day=day_string,
                held_since=None,
            ),
            "increase",
        )

    # The counter decreased. Did the portal's own self-reported date also
    # advance? If so this is a genuine new accumulation period, not a
    # glitch - the previous day's growth is already folded into
    # `accumulated` via each poll's incremental delta, so there is nothing
    # to "finalize"; just add on however far the new day has already
    # progressed and move the baseline forward.
    if (
        day_string is not None
        and state.last_portal_day is not None
        and day_string != state.last_portal_day
    ):
        return (
            replace(
                state,
                accumulated=state.accumulated + max(raw, 0.0),
                last_raw=raw,
                last_portal_day=day_string,
                held_since=None,
            ),
            "confirmed_rollover",
        )

    # Same portal day, but the value went down - looks like a transient
    # glitch rather than a rollover. Hold the previous (higher) baseline so
    # that if the next poll recovers, nothing gets double-counted. Force-
    # accept after a bounded timeout so a persistently wrong reading can
    # never get the tracker stuck forever.
    held_since = state.held_since or now
    if now - held_since >= LIFETIME_SUSPICIOUS_HOLD_TIMEOUT:
        return (
            replace(state, last_raw=raw, last_portal_day=day_string, held_since=None),
            "suspicious_force_accepted",
        )

    return replace(state, held_since=held_since), "suspicious_held"


@dataclass
class _LifetimeExtraStoredData(ExtraStoredData):
    """Restore-state payload: the running total plus enough context to resume safely."""

    accumulated: float
    last_raw: float | None
    last_portal_day: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "accumulated": self.accumulated,
            "last_raw": self.last_raw,
            "last_portal_day": self.last_portal_day,
        }

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> _LifetimeExtraStoredData | None:
        try:
            return cls(
                accumulated=float(restored["accumulated"]),
                last_raw=restored.get("last_raw"),
                last_portal_day=restored.get("last_portal_day"),
            )
        except (KeyError, TypeError, ValueError):
            return None


class ApsystemsLifetimeEnergySensor(
    CoordinatorEntity[ApsystemsCloudCrawlerCoordinator], RestoreEntity, SensorEntity
):
    """A synthesized, never-resetting lifetime total for one daily energy counter."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        coordinator: ApsystemsCloudCrawlerCoordinator,
        entry: ConfigEntry,
        meta: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._source_key = meta["source"]
        self._attr_name = meta["name"]
        self._attr_translation_key = meta["key"]
        self._attr_unique_id = f"{entry.entry_id}_{meta['key']}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
        self._state = _TrackerState()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (restored := await self.async_get_last_extra_data()) is not None:
            parsed = _LifetimeExtraStoredData.from_dict(restored.as_dict())
            if parsed is not None:
                self._state = _TrackerState(
                    accumulated=parsed.accumulated,
                    last_raw=parsed.last_raw,
                    last_portal_day=parsed.last_portal_day,
                )
        self._process_coordinator_data()

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        return _LifetimeExtraStoredData(
            accumulated=self._state.accumulated,
            last_raw=self._state.last_raw,
            last_portal_day=self._state.last_portal_day,
        )

    @property
    def native_value(self) -> float | None:
        return self._state.accumulated

    def _process_coordinator_data(self) -> None:
        data = self.coordinator.data
        if data is None:
            return
        raw = getattr(data, self._source_key, None)
        raw_last_report_time = data.raw_control_info.get("lastReportTime")
        day_string = day_string_from_report_time(raw_last_report_time)
        previous_raw = self._state.last_raw
        new_state, status = advance_tracker(self._state, raw, day_string, dt_util.utcnow())
        self._state = new_state
        if status == "suspicious_held":
            _LOGGER.warning(
                "%s: holding a same-day decrease (%.3f -> %.3f kWh) that doesn't "
                "look like a rollover - will keep the higher baseline until it "
                "recovers or %s passes. lastReportTime=%s",
                self._attr_name,
                previous_raw,
                raw,
                LIFETIME_SUSPICIOUS_HOLD_TIMEOUT,
                raw_last_report_time,
            )
        elif status == "suspicious_force_accepted":
            _LOGGER.warning(
                "%s: a same-day decrease (%.3f -> %.3f kWh) could not be "
                "resolved as a rollover within %s - accepting the lower value "
                "as the new baseline to avoid getting stuck. lastReportTime=%s",
                self._attr_name,
                previous_raw,
                raw,
                LIFETIME_SUSPICIOUS_HOLD_TIMEOUT,
                raw_last_report_time,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._process_coordinator_data()
        super()._handle_coordinator_update()


def create_lifetime_energy_sensors(
    coordinator: ApsystemsCloudCrawlerCoordinator, entry: ConfigEntry
) -> list[ApsystemsLifetimeEnergySensor]:
    """Build one lifetime sensor per entry in LIFETIME_ENERGY_SENSORS."""
    return [
        ApsystemsLifetimeEnergySensor(coordinator, entry, meta)
        for meta in LIFETIME_ENERGY_SENSORS.values()
    ]
