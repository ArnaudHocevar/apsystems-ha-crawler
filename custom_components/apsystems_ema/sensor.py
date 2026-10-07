"""Sensor platform for the APsystems EMA integration."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_GENERATOR_SENSORS, DAILY_ENERGY_SENSORS, DOMAIN, MANUFACTURER, MODEL
from .coordinator import ApsystemsEmaCoordinator
from .models import EmaData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ApsystemsEmaSensorDescription(SensorEntityDescription):
    """Describes an APsystems EMA sensor entity."""

    value_fn: Callable[[EmaData], object] = lambda data: None
    extra_attrs_fn: Callable[[EmaData], dict] | None = None


def _storage_status_attrs(data: EmaData) -> dict:
    attrs: dict = {}
    if data.abd:
        # Only ever expose the first battery pack entry for now; multi-pack
        # accounts are not supported in v1 (see models.py parse_control_info).
        attrs["abd"] = data.abd
    return attrs


SENSOR_DESCRIPTIONS: tuple[ApsystemsEmaSensorDescription, ...] = (
    ApsystemsEmaSensorDescription(
        key="grid_power",
        translation_key="grid_power",
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.grid_power,
    ),
    ApsystemsEmaSensorDescription(
        key="load_power",
        translation_key="load_power",
        name="Load Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.load_power,
    ),
    ApsystemsEmaSensorDescription(
        key="pv_power",
        translation_key="pv_power",
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.pv_power,
    ),
    ApsystemsEmaSensorDescription(
        key="storage_bat_power",
        translation_key="storage_bat_power",
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.storage_bat_power,
    ),
    ApsystemsEmaSensorDescription(
        key="storage_soc",
        translation_key="storage_soc",
        name="Battery State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.storage_soc,
        extra_attrs_fn=_storage_status_attrs,
    ),
    ApsystemsEmaSensorDescription(
        key="storage_status",
        translation_key="storage_status",
        name="Battery Status Code",
        # Raw numeric status code from the portal. The meaning of individual
        # codes (0/1/2/...) is not yet confirmed - TODO: map these to
        # human-readable states (e.g. idle/charging/discharging) once
        # confirmed against more observations. For now we just pass the
        # integer through.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.storage_status,
    ),
    ApsystemsEmaSensorDescription(
        key="storage_capacity",
        translation_key="storage_capacity",
        name="Battery Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.storage_capacity,
    ),
    ApsystemsEmaSensorDescription(
        key="last_report_time",
        translation_key="last_report_time",
        name="Last Report Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_report_time,
    ),
    # Lifetime counters from getDashboardSummaryInfoAjax - genuine monotonic
    # server-side totals, complementary to the DE2/DE3 daily counters below.
    ApsystemsEmaSensorDescription(
        key="pv_lifetime_energy",
        translation_key="pv_lifetime_energy",
        name="Solar Production Lifetime",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.pv_lifetime_energy,
    ),
    ApsystemsEmaSensorDescription(
        key="consume_lifetime_energy",
        translation_key="consume_lifetime_energy",
        name="Consumption Lifetime",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.consume_lifetime_energy,
    ),
    # Battery/grid strategy settings (slow-polled, see coordinator.py).
    # Field semantics are best-effort reverse-engineered guesses - see
    # models.parse_strategy for details and caveats.
    ApsystemsEmaSensorDescription(
        key="strategy_mode",
        translation_key="strategy_mode",
        name="Battery Strategy Mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.strategy_mode,
    ),
    ApsystemsEmaSensorDescription(
        key="backup_reserve_soc",
        translation_key="backup_reserve_soc",
        name="Backup Reserve SOC",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.backup_reserve_soc,
    ),
    ApsystemsEmaSensorDescription(
        key="peak_shaving_threshold",
        translation_key="peak_shaving_threshold",
        name="Peak Shaving Threshold",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.peak_shaving_threshold,
    ),
)

# Generator sensors - only set up when the enable_generator_sensors option is
# on (see coordinator.py / config_flow.py). Field semantics are best-effort
# reverse-engineered guesses, not confirmed APsystems documentation; fields
# too ambiguous to expose as dedicated entities remain available via
# raw_generator_realtime as an attribute on the status sensor.
GENERATOR_SENSOR_DESCRIPTIONS: tuple[ApsystemsEmaSensorDescription, ...] = (
    ApsystemsEmaSensorDescription(
        key="generator_power",
        translation_key="generator_power",
        name="Generator Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.generator_power,
    ),
    ApsystemsEmaSensorDescription(
        key="generator_voltage",
        translation_key="generator_voltage",
        name="Generator Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.generator_voltage,
    ),
    ApsystemsEmaSensorDescription(
        key="generator_frequency",
        translation_key="generator_frequency",
        name="Generator Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.generator_frequency,
    ),
    ApsystemsEmaSensorDescription(
        key="generator_temperature",
        translation_key="generator_temperature",
        name="Generator Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.generator_temperature,
    ),
    ApsystemsEmaSensorDescription(
        key="generator_status",
        translation_key="generator_status",
        name="Generator Status Code",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.generator_status,
        extra_attrs_fn=lambda data: dict(data.raw_generator_realtime),
    ),
)

# The six daily-resetting energy counters (DE0-DE5). These are plain,
# ordinary total_increasing sensors; HA's recorder handles the midnight
# reset natively. The integration does NOT add these to the Energy
# dashboard automatically - the user picks which entities to use there
# themselves. See statistics.py for the separate, integration-owned
# external-statistics backfill that complements (but never replaces or
# overwrites) these entities' own sensor-backed statistics.
DAILY_ENERGY_DESCRIPTIONS: tuple[ApsystemsEmaSensorDescription, ...] = tuple(
    ApsystemsEmaSensorDescription(
        key=meta["key"],
        translation_key=meta["key"],
        name=meta["name"],
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data, attr=meta["key"]: getattr(data, attr),
    )
    for meta in DAILY_ENERGY_SENSORS.values()
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up APsystems EMA sensors from a config entry."""
    coordinator: ApsystemsEmaCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    descriptions = list(SENSOR_DESCRIPTIONS) + list(DAILY_ENERGY_DESCRIPTIONS)
    if entry.options.get(CONF_ENABLE_GENERATOR_SENSORS, False):
        descriptions.extend(GENERATOR_SENSOR_DESCRIPTIONS)

    entities = [
        ApsystemsEmaSensor(coordinator, entry, description) for description in descriptions
    ]
    async_add_entities(entities)


class ApsystemsEmaSensor(CoordinatorEntity[ApsystemsEmaCoordinator], SensorEntity):
    """Representation of a single APsystems EMA sensor."""

    entity_description: ApsystemsEmaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ApsystemsEmaCoordinator,
        entry: ConfigEntry,
        description: ApsystemsEmaSensorDescription,
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
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.coordinator.data is None or self.entity_description.extra_attrs_fn is None:
            return None
        return self.entity_description.extra_attrs_fn(self.coordinator.data)
