"""DataUpdateCoordinator for the APsystems EMA integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApsystemsEmaAuthError, ApsystemsEmaClient, ApsystemsEmaConnectionError
from .const import CONF_ENABLE_GENERATOR_SENSORS, DOMAIN, SLOW_POLL_INTERVAL
from .models import EmaData, build_ema_data

_LOGGER = logging.getLogger(__name__)


class ApsystemsEmaCoordinator(DataUpdateCoordinator[EmaData]):
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
        client: ApsystemsEmaClient,
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

    async def _async_fetch_live(self) -> tuple[dict, dict, dict]:
        control_info_raw = await self.client.async_get_control_info()
        storage_summary_raw = await self.client.async_get_storage_summary()
        dashboard_summary_raw = await self.client.async_get_dashboard_summary()
        return control_info_raw, storage_summary_raw, dashboard_summary_raw

    async def _async_maybe_fetch_strategy(self) -> None:
        now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
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

    async def _async_update_data(self) -> EmaData:
        try:
            if not self._logged_in:
                await self.client.async_login()
                self._logged_in = True

            try:
                control_info_raw, storage_summary_raw, dashboard_summary_raw = (
                    await self._async_fetch_live()
                )
            except ApsystemsEmaAuthError:
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
            except ApsystemsEmaAuthError:
                # Non-fatal for this cycle: keep the live data we already have
                # and simply retry strategy/generator polling next cycle.
                _LOGGER.debug(
                    "Strategy/generator poll failed with an auth error, will retry next cycle"
                )

            return build_ema_data(
                control_info_raw,
                storage_summary_raw,
                dashboard_summary_raw,
                self._strategy_info_raw,
                self._system_strategy_raw,
                self._generator_data_raw,
                self._generator_realtime_raw,
            )
        except ApsystemsEmaAuthError as err:
            self._logged_in = False
            raise ConfigEntryAuthFailed(str(err)) from err
        except ApsystemsEmaConnectionError as err:
            raise UpdateFailed(str(err)) from err
