"""Base entity for Powerpal Direct."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import MANUFACTURER
from .coordinator import PowerpalCoordinator


class PowerpalEntity(CoordinatorEntity[PowerpalCoordinator]):
    """An entity on the Powerpal device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerpalCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            name=coordinator.config_entry.title,
        )
