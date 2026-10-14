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
    # The platform's domain, to check a gateway's entity ID belongs to the same one.
    _entity_domain: str

    def __init__(self, coordinator: PowerpalCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"
        # Take over a removed ESP32 gateway's entity ID, so its history carries on. Only
        # used when this entity isn't registered yet, and the ID is known to be free.
        inherited = coordinator.gateway.available.get(description.key)
        if inherited is not None and inherited.startswith(f"{self._entity_domain}."):
            self.entity_id = inherited
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            name=coordinator.config_entry.title,
        )
