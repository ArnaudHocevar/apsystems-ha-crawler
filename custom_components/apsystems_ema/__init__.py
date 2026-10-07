"""The APsystems EMA integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import homeassistant.util.dt as dt_util

from .api import ApsystemsEmaAuthError, ApsystemsEmaClient, ApsystemsEmaConnectionError
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUAL_BACKFILL_MAX_DAYS,
    MANUAL_BACKFILL_PACE_SECONDS,
    MIN_SCAN_INTERVAL,
)
from .coordinator import ApsystemsEmaCoordinator
from .statistics import async_backfill_date_range, async_backfill_statistics

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_BACKFILL = "backfill"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_FORCE = "force"

BACKFILL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): selector.ConfigEntrySelector(
            selector.ConfigEntrySelectorConfig(integration=DOMAIN)
        ),
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up APsystems EMA from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    # Each config entry gets its own dedicated aiohttp session (still backed
    # by HA's aiohttp connector infrastructure via async_create_clientsession,
    # fully async - no `requests`), rather than reusing HA's single shared
    # client session. The EMA portal authenticates purely via cookies, and a
    # shared session's cookie jar would be a single global jar: with two EMA
    # accounts configured, their session cookies would collide and
    # continuously log each other out. A dedicated session per entry avoids
    # that while still being a proper async aiohttp client.
    session = async_create_clientsession(hass)
    client = ApsystemsEmaClient(session, username, password)

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    scan_interval = max(scan_interval, MIN_SCAN_INTERVAL)

    coordinator = ApsystemsEmaCoordinator(
        hass, entry, client, update_interval=timedelta(seconds=scan_interval)
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        await session.close()
        raise
    except Exception:
        await session.close()
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "session": session,
        "client": client,
    }

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Backfill external statistics for the daily energy counters. This is a
    # nice-to-have: failures here must never block setup or affect the
    # primary sensors, so they are caught and logged rather than raised.
    try:
        await async_backfill_statistics(hass, entry.entry_id, entry.title, client)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("APsystems EMA statistics backfill failed, continuing without it")

    _async_register_backfill_service(hass)

    return True


def _async_register_backfill_service(hass: HomeAssistant) -> None:
    """Register the apsystems_ema.backfill service (once, shared across all config entries)."""
    if hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        return

    async def _async_handle_backfill(call: ServiceCall) -> None:
        """Handle a user-invoked apsystems_ema.backfill service call."""
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
        if entry is None or entry_data is None:
            raise HomeAssistantError(
                f"Unknown or not-loaded APsystems EMA config entry: {entry_id}"
            )

        start_date = call.data[ATTR_START_DATE]
        end_date = call.data.get(ATTR_END_DATE, dt_util.now().date())
        if end_date < start_date:
            raise HomeAssistantError("end_date must not be before start_date")

        span_days = (end_date - start_date).days + 1
        if span_days > MANUAL_BACKFILL_MAX_DAYS:
            raise HomeAssistantError(
                f"Requested range of {span_days} days exceeds the "
                f"{MANUAL_BACKFILL_MAX_DAYS}-day safety cap for a single backfill "
                "call; split the request into smaller ranges."
            )

        client: ApsystemsEmaClient = entry_data["client"]
        force = call.data.get(ATTR_FORCE, False)
        _LOGGER.info(
            "Starting manual APsystems EMA backfill for %s: %s to %s (%d day(s))%s",
            entry.title,
            start_date,
            end_date,
            span_days,
            " [force overwrite]" if force else "",
        )
        try:
            results = await async_backfill_date_range(
                hass,
                entry.entry_id,
                entry.title,
                client,
                start_date,
                end_date,
                pace_seconds=MANUAL_BACKFILL_PACE_SECONDS,
                force=force,
            )
        except (ApsystemsEmaAuthError, ApsystemsEmaConnectionError) as err:
            raise HomeAssistantError(f"APsystems EMA backfill failed: {err}") from err
        _LOGGER.info(
            "Manual APsystems EMA backfill for %s complete: %s", entry.title, results
        )
        if not any(results.values()):
            _LOGGER.warning(
                "Manual APsystems EMA backfill for %s wrote 0 days for every "
                "counter over %s to %s - this usually means the portal has "
                "no data for that range (outside its retention window, or a "
                "date before the EMA account/plant existed), not a failed "
                "call. See earlier per-day warnings, if any, for details. "
                "Also remember: this writes to a separate 'apsystems_ema:...' "
                "statistic distinct from the native sensor entities - see "
                "the README's Energy dashboard section for which one to add "
                "as your Energy source to see this history.",
                entry.title,
                start_date,
                end_date,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_BACKFILL, _async_handle_backfill, schema=BACKFILL_SERVICE_SCHEMA
    )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update (e.g. scan_interval change) by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["session"].close()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_BACKFILL)
    return unload_ok
