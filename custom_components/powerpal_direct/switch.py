"""Switch for Powerpal Direct."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import PowerpalConfigEntry
from .entity import PowerpalEntity

CONNECTION = SwitchEntityDescription(key="connection", translation_key="connection")
CLOUD_UPLOAD = SwitchEntityDescription(
    key="cloud_upload", translation_key="cloud_upload", entity_category=EntityCategory.CONFIG
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        [
            PowerpalConnectionSwitch(entry.runtime_data, CONNECTION),
            PowerpalCloudUploadSwitch(entry.runtime_data, CLOUD_UPLOAD),
        ]
    )


class PowerpalConnectionSwitch(PowerpalEntity, SwitchEntity, RestoreEntity):
    """Off lets go of the Powerpal, e.g. so the phone app or an ESP32 gateway can connect.

    Stays as set across restarts, unlike the gateway's switch, so that running an ESP32
    alongside isn't undone by a Home Assistant restart. On unless turned off.
    """

    _entity_domain = "switch"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Before the connection loop starts, so a switch left off never connects at all.
        if (last := await self.async_get_last_state()) is not None and last.state == STATE_OFF:
            self.coordinator.connection_enabled = False

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


class PowerpalCloudUploadSwitch(PowerpalEntity, SwitchEntity, RestoreEntity):
    """Send readings to Powerpal's cloud, as the phone app would. Off unless turned on, and
    stays as set across restarts. Turning it off also drops anything not yet sent."""

    _entity_domain = "switch"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.coordinator.async_set_cloud_upload(last is not None and last.state == STATE_ON)

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.cloud.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_cloud_upload(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_cloud_upload(False)
