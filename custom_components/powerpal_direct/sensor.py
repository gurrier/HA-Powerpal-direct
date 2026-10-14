"""Sensors for Powerpal Direct."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerpalConfigEntry, PowerpalCoordinator
from .entity import PowerpalEntity


@dataclass(frozen=True, kw_only=True)
class PowerpalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PowerpalCoordinator], float | int | str | datetime | None]
    available_fn: Callable[[PowerpalCoordinator], bool] = lambda c: True


SENSORS: tuple[PowerpalSensorDescription, ...] = (
    # Named, classed and categorised like the ESP32 gateway's entities, so that taking over
    # their entity IDs carries their history and statistics on without conflicts.
    PowerpalSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda c: c.data.power_w,
        available_fn=lambda c: c.power_available,
    ),
    PowerpalSensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda c: c.total_kwh,
    ),
    PowerpalSensorDescription(
        key="daily_energy",
        translation_key="daily_energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda c: c.daily_kwh,
    ),
    PowerpalSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.battery,
    ),
    PowerpalSensorDescription(
        key="led_sensitivity",
        translation_key="led_sensitivity",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.led_sensitivity,
    ),
    PowerpalSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.last_record,
    ),
    PowerpalSensorDescription(
        key="last_pulses",
        translation_key="last_pulses",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.last_pulses,
    ),
    PowerpalSensorDescription(
        key="last_watt_hours",
        translation_key="last_watt_hours",
        # Not the energy device class: that's for totals, and this is one reading's worth.
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.last_watt_hours,
    ),
    PowerpalSensorDescription(
        key="last_cost",
        translation_key="last_cost",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=6,
        value_fn=lambda c: c.last_cost,
    ),
    PowerpalSensorDescription(
        key="device_id",
        translation_key="device_id",
        value_fn=lambda c: c.data.identity.device_id if c.data.identity else None,
    ),
    PowerpalSensorDescription(
        key="last_cloud_upload",
        translation_key="last_cloud_upload",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.cloud.last_upload,
    ),
    PowerpalSensorDescription(
        key="advertisement_rssi",
        translation_key="advertisement_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.advertisement_rssi,
    ),
    PowerpalSensorDescription(
        key="advertisement_rate",
        translation_key="advertisement_rate",
        native_unit_of_measurement="ads/min",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.advertisement_rate,
    ),
    PowerpalSensorDescription(
        key="clock_offset",
        translation_key="clock_offset",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.clock_offset,
    ),
    PowerpalSensorDescription(
        key="connected_via",
        translation_key="connected_via",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.connected_via,
    ),
    PowerpalSensorDescription(
        key="last_stall",
        translation_key="last_stall",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.last_stall,
    ),
    PowerpalSensorDescription(
        key="history_oldest",
        translation_key="history_oldest",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.data.history_oldest,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        (PowerpalLastStallSensor if description.key == "last_stall" else PowerpalSensor)(coordinator, description)
        for description in SENSORS
    )


class PowerpalSensor(PowerpalEntity, SensorEntity):
    entity_description: PowerpalSensorDescription
    _entity_domain = "sensor"

    def __init__(self, coordinator: PowerpalCoordinator, description: PowerpalSensorDescription) -> None:
        super().__init__(coordinator, description)
        if description.key == "last_cost":
            self._attr_native_unit_of_measurement = coordinator.hass.config.currency

    @property
    def available(self) -> bool:
        return self.entity_description.available_fn(self.coordinator)

    @property
    def native_value(self) -> float | int | str | datetime | None:
        return self.entity_description.value_fn(self.coordinator)


class PowerpalLastStallSensor(PowerpalSensor, RestoreSensor):
    """Kept across restarts, like the gateway's, since stalls are rare and worth remembering."""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.data.last_stall is None and (last := await self.async_get_last_sensor_data()):
            self.coordinator.data.last_stall = last.native_value  # type: ignore[assignment]
