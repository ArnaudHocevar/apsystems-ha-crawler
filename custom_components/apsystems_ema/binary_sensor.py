"""Binary sensor platform for the APsystems EMA integration.

Optional, low-priority entities derived from getDashboardSummaryInfoAjax.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ApsystemsEmaCoordinator
from .models import EmaData


@dataclass(frozen=True, kw_only=True)
class ApsystemsEmaBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an APsystems EMA binary sensor entity."""

    value_fn: Callable[[EmaData], bool | None] = lambda data: None


BINARY_SENSOR_DESCRIPTIONS: tuple[ApsystemsEmaBinarySensorDescription, ...] = (
    ApsystemsEmaBinarySensorDescription(
        key="running_status",
        translation_key="running_status",
        name="System Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: None if data.running_status is None else data.running_status == 1,
    ),
    ApsystemsEmaBinarySensorDescription(
        key="communication_status",
        translation_key="communication_status",
        name="System Communication OK",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda data: (
            None if data.communication_status is None else data.communication_status == 1
        ),
    ),
    # Best-effort guesses from getStrategyInfoWithMenu/getSystemStrategy, see
    # models.parse_strategy.
    ApsystemsEmaBinarySensorDescription(
        key="eps_enabled",
        translation_key="eps_enabled",
        name="EPS Enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.eps_enabled,
    ),
    ApsystemsEmaBinarySensorDescription(
        key="zero_export_enabled",
        translation_key="zero_export_enabled",
        name="Zero Export Enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.zero_export_enabled,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up APsystems EMA binary sensors from a config entry."""
    coordinator: ApsystemsEmaCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        ApsystemsEmaBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ApsystemsEmaBinarySensor(CoordinatorEntity[ApsystemsEmaCoordinator], BinarySensorEntity):
    """Representation of a single APsystems EMA binary sensor."""

    entity_description: ApsystemsEmaBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ApsystemsEmaCoordinator,
        entry: ConfigEntry,
        description: ApsystemsEmaBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
