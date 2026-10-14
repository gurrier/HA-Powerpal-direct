"""Sensors for Powerpal Direct."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerpalConfigEntry, PowerpalCoordinator
from .entity import PowerpalEntity


@dataclass(frozen=True, kw_only=True)
class PowerpalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PowerpalCoordinator], float | int | str | datetime | None]
    available_fn: Callable[[PowerpalCoordinator], bool] = lambda c: True


SENSORS: tuple[PowerpalSensorDescription, ...] = (
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
        key="last_pulses",
        translation_key="last_pulses",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.data.last_pulses,
    ),
    PowerpalSensorDescription(
        key="last_reading",
        translation_key="last_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.last_record,
    ),
    PowerpalSensorDescription(
        key="history_oldest",
        translation_key="history_oldest",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.data.history_oldest,
    ),
    PowerpalSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.battery,
    ),
    PowerpalSensorDescription(
        key="led_sensitivity",
        translation_key="led_sensitivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.led_sensitivity,
    ),
    PowerpalSensorDescription(
        key="api_key",
        translation_key="api_key",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.data.identity.api_key if c.data.identity else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(PowerpalSensor(coordinator, description) for description in SENSORS)


class PowerpalSensor(PowerpalEntity, SensorEntity):
    entity_description: PowerpalSensorDescription

    @property
    def available(self) -> bool:
        return self.entity_description.available_fn(self.coordinator)

    @property
    def native_value(self) -> float | int | str | datetime | None:
        return self.entity_description.value_fn(self.coordinator)
