"""Config flow for the APsystems EMA integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import ApsystemsEmaAuthError, ApsystemsEmaClient, ApsystemsEmaConnectionError
from .const import (
    CONF_ENABLE_GENERATOR_SENSORS,
    CONF_SCAN_INTERVAL,
    DEFAULT_ENABLE_GENERATOR_SENSORS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _async_validate_login(hass: HomeAssistant, username: str, password: str) -> None:
    """Perform a real login to validate credentials, raising on failure.

    Uses a short-lived, dedicated aiohttp session (not the shared HA session)
    so the validation's cookie jar can never leak into / collide with another
    config entry's authenticated session.
    """
    session = async_create_clientsession(hass)
    try:
        client = ApsystemsEmaClient(session, username, password)
        await client.async_login()
    finally:
        await session.close()


class ApsystemsEmaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for APsystems EMA."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()

            try:
                await _async_validate_login(self.hass, username, password)
            except ApsystemsEmaAuthError:
                errors["base"] = "invalid_auth"
            except ApsystemsEmaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error validating APsystems EMA credentials")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=username, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ApsystemsEmaOptionsFlow:
        return ApsystemsEmaOptionsFlow(config_entry)


class ApsystemsEmaOptionsFlow(config_entries.OptionsFlow):
    """Options flow allowing the user to tune the polling interval and toggle
    the (experimental, field-semantics-unconfirmed) generator sensors."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            scan_interval = user_input[CONF_SCAN_INTERVAL]
            if scan_interval < MIN_SCAN_INTERVAL:
                errors[CONF_SCAN_INTERVAL] = "scan_interval_too_low"
            else:
                return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self._config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        current_generator = self._config_entry.options.get(
            CONF_ENABLE_GENERATOR_SENSORS, DEFAULT_ENABLE_GENERATOR_SENSORS
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)
                ),
                vol.Required(
                    CONF_ENABLE_GENERATOR_SENSORS, default=current_generator
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
