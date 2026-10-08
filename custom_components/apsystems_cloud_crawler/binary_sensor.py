"""Binary sensor platform for the APSystems Cloud Crawler integration.

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
from .coordinator import ApsystemsCloudCrawlerCoordinator
from .models import CloudCrawlerData


@dataclass(frozen=True, kw_only=True)
class ApsystemsCloudCrawlerBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an APSystems Cloud Crawler binary sensor entity."""

    value_fn: Callable[[CloudCrawlerData], bool | None] = lambda data: None


BINARY_SENSOR_DESCRIPTIONS: tuple[ApsystemsCloudCrawlerBinarySensorDescription, ...] = (
    ApsystemsCloudCrawlerBinarySensorDescription(
        key="running_status",
        translation_key="running_status",
        name="System Running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: None if data.running_status is None else data.running_status == 1,
    ),
    ApsystemsCloudCrawlerBinarySensorDescription(
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
    ApsystemsCloudCrawlerBinarySensorDescription(
        key="eps_enabled",
        translation_key="eps_enabled",
        name="EPS Enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.eps_enabled,
    ),
    ApsystemsCloudCrawlerBinarySensorDescription(
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
    """Set up APSystems Cloud Crawler binary sensors from a config entry."""
    coordinator: ApsystemsCloudCrawlerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        ApsystemsCloudCrawlerBinarySensor(coordinator, entry, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class ApsystemsCloudCrawlerBinarySensor(
    CoordinatorEntity[ApsystemsCloudCrawlerCoordinator], BinarySensorEntity
):
    """Representation of a single APSystems Cloud Crawler binary sensor."""

    entity_description: ApsystemsCloudCrawlerBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ApsystemsCloudCrawlerCoordinator,
        entry: ConfigEntry,
        description: ApsystemsCloudCrawlerBinarySensorDescription,
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
