"""Binary sensors for Powerpal Direct."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerpalConfigEntry
from .entity import PowerpalEntity

CONNECTED = BinarySensorEntityDescription(
    key="connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PowerpalConnectedSensor(entry.runtime_data, CONNECTED)])


class PowerpalConnectedSensor(PowerpalEntity, BinarySensorEntity):
    """Whether Home Assistant currently holds the Powerpal's single connection."""

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.connected
