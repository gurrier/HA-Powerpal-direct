"""Connect Home Assistant directly to a Powerpal energy monitor over Bluetooth."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION
from .coordinator import PowerpalConfigEntry, PowerpalCoordinator
from .gateway import async_adopt_gateway_entities, async_take_over_entity_ids, find_gateway_entities
from .hourly import async_clear_hourly_statistic

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    coordinator = PowerpalCoordinator(hass, entry)
    # Before the entities are added, so they can take over a removed gateway's entity IDs.
    coordinator.gateway = find_gateway_entities(hass)
    entry.runtime_data = coordinator
    await coordinator.async_load()
    if coordinator.first_setup and coordinator.gateway.adoptable:
        # The gateway's ESPHome entry is disabled: take its entity records over whole.
        async_adopt_gateway_entities(hass, entry, coordinator.address, coordinator.gateway)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if coordinator.first_setup:
        # Only on first setup, so an existing install's entity IDs never change under it.
        async_take_over_entity_ids(hass, coordinator.address, DOMAIN, coordinator.gateway)
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> None:
    """Forget the energy totals along with the entry."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cloud").async_remove()
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.hourly").async_remove()
    if entry.unique_id:
        async_clear_hourly_statistic(hass, entry.unique_id)


async def _async_update_listener(hass: HomeAssistant, entry: PowerpalConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
