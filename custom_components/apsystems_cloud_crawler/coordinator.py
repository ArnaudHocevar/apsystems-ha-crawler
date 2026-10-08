"""DataUpdateCoordinator for the APSystems Cloud Crawler integration."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ApsystemsCloudCrawlerAuthError,
    ApsystemsCloudCrawlerClient,
    ApsystemsCloudCrawlerConnectionError,
)
from .const import CONF_ENABLE_GENERATOR_SENSORS, DAILY_ENERGY_SENSORS, DOMAIN, SLOW_POLL_INTERVAL
from .daily_energy_guard import DailyEnergyRolloverGuard
from .models import CloudCrawlerData, build_cloud_crawler_data

_LOGGER = logging.getLogger(__name__)


class ApsystemsCloudCrawlerCoordinator(DataUpdateCoordinator[CloudCrawlerData]):
    """Coordinates polling of the EMA portal's dashboard endpoints.

    The live control-info/storage-summary/dashboard-summary endpoints are
    fetched on every refresh cycle (cheap, per the reverse-engineering
    notes). The battery/grid strategy and (optional) generator endpoints
    change rarely and are instead refreshed on their own slower cadence
    (``SLOW_POLL_INTERVAL``), with the last fetched raw payloads cached and
    reused on cycles where they are not re-polled.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ApsystemsCloudCrawlerClient,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )
        self.entry = entry
        self.client = client
        self._logged_in = False

        self._strategy_info_raw: dict | None = None
        self._system_strategy_raw: dict | None = None
        self._last_strategy_poll: datetime | None = None

        self._generator_data_raw: dict | None = None
        self._generator_realtime_raw: dict | None = None
        self._last_generator_poll: datetime | None = None

        self._daily_energy_guard = DailyEnergyRolloverGuard()

    async def _async_fetch_live(self) -> tuple[dict, dict, dict]:
        control_info_raw = await self.client.async_get_control_info()
        storage_summary_raw = await self.client.async_get_storage_summary()
        dashboard_summary_raw = await self.client.async_get_dashboard_summary()
        return control_info_raw, storage_summary_raw, dashboard_summary_raw

    async def _async_maybe_fetch_strategy(self) -> None:
        now = datetime.now(UTC)
        if (
            self._last_strategy_poll is not None
            and now - self._last_strategy_poll < SLOW_POLL_INTERVAL
        ):
            return
        self._strategy_info_raw = await self.client.async_get_strategy_info()
        self._system_strategy_raw = await self.client.async_get_system_strategy()
        self._last_strategy_poll = now

    async def _async_maybe_fetch_generator(self, ecu_dev_id: str | None) -> None:
        if not self.entry.options.get(CONF_ENABLE_GENERATOR_SENSORS, False):
            return
        if not ecu_dev_id:
            return
        now = datetime.now(UTC)
        if (
            self._last_generator_poll is not None
            and now - self._last_generator_poll < SLOW_POLL_INTERVAL
        ):
            return
        self._generator_data_raw = await self.client.async_get_generator_data(ecu_dev_id)
        self._generator_realtime_raw = await self.client.async_get_generator_realtime(
            ecu_dev_id
        )
        self._last_generator_poll = now

    async def _async_filter_daily_energy(
        self, data: CloudCrawlerData, control_info_raw: dict
    ) -> None:
        """Filter the six daily energy counters through the rollover guard.

        Mutates ``data`` in place, replacing each DE0-DE5-derived attribute
        with the guard's verdict - see daily_energy_guard.py for why a raw
        decrease from the portal cannot be trusted as-is.
        """
        raw_values = {
            de_key: getattr(data, meta["key"]) for de_key, meta in DAILY_ENERGY_SENSORS.items()
        }
        filtered = await self._daily_energy_guard.async_filter(
            self.client,
            raw_values,
            control_info_raw.get("lastReportTime"),
            datetime.now(UTC),
        )
        for de_key, meta in DAILY_ENERGY_SENSORS.items():
            setattr(data, meta["key"], filtered.get(de_key))

    async def _async_update_data(self) -> CloudCrawlerData:
        try:
            if not self._logged_in:
                await self.client.async_login()
                self._logged_in = True

            try:
                control_info_raw, storage_summary_raw, dashboard_summary_raw = (
                    await self._async_fetch_live()
                )
            except ApsystemsCloudCrawlerAuthError:
                # Session likely expired between polls even though client-level
                # retry already attempts one re-login; try a full fresh cycle
                # once more before giving up for this update.
                self._logged_in = False
                await self.client.async_login()
                self._logged_in = True
                control_info_raw, storage_summary_raw, dashboard_summary_raw = (
                    await self._async_fetch_live()
                )

            # ABID (first battery pack's device id) doubles as the ecuDevId
            # param for the generator endpoints; only the first entry is used
            # (see models.parse_control_info for the multi-pack caveat).
            abd = []
            try:
                import json as _json

                abd = _json.loads(control_info_raw.get("ABD") or "[]")
            except (ValueError, TypeError):
                abd = []
            ecu_dev_id = abd[0].get("ABID") if abd else None

            try:
                await self._async_maybe_fetch_strategy()
                await self._async_maybe_fetch_generator(ecu_dev_id)
            except ApsystemsCloudCrawlerAuthError:
                # Non-fatal for this cycle: keep the live data we already have
                # and simply retry strategy/generator polling next cycle.
                _LOGGER.debug(
                    "Strategy/generator poll failed with an auth error, will retry next cycle"
                )

            data = build_cloud_crawler_data(
                control_info_raw,
                storage_summary_raw,
                dashboard_summary_raw,
                self._strategy_info_raw,
                self._system_strategy_raw,
                self._generator_data_raw,
                self._generator_realtime_raw,
            )
            await self._async_filter_daily_energy(data, control_info_raw)
            return data
        except ApsystemsCloudCrawlerAuthError as err:
            self._logged_in = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except ApsystemsCloudCrawlerConnectionError as err:
            raise UpdateFailed(str(err)) from err
