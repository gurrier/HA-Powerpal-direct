"""Switch for Powerpal Direct."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerpalConfigEntry
from .entity import PowerpalEntity

CONNECTION = SwitchEntityDescription(
    key="connection",
    translation_key="connection",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([PowerpalConnectionSwitch(entry.runtime_data, CONNECTION)])


class PowerpalConnectionSwitch(PowerpalEntity, SwitchEntity):
    """Off lets go of the Powerpal, e.g. so the phone app can connect, until turned back on."""

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connection_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(False)
