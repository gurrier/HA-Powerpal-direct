"""Buttons for Powerpal Direct."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerpalConfigEntry, PowerpalCoordinator
from .entity import PowerpalEntity


@dataclass(frozen=True, kw_only=True)
class PowerpalButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[PowerpalCoordinator], Awaitable[None]]


BUTTONS: tuple[PowerpalButtonDescription, ...] = (
    PowerpalButtonDescription(
        key="reconnect",
        translation_key="reconnect",
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda c: c.async_reconnect(),
    ),
    # Off by default: the clock is already set on every connect and every 6 hours.
    PowerpalButtonDescription(
        key="set_time",
        translation_key="set_time",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=lambda c: c.async_set_device_time(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerpalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(PowerpalButton(entry.runtime_data, description) for description in BUTTONS)


class PowerpalButton(PowerpalEntity, ButtonEntity):
    entity_description: PowerpalButtonDescription
    _entity_domain = "button"

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
