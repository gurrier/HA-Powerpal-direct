"""Setting up the entry end to end, against a fake Powerpal."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.powerpal_direct.const import (
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)
from custom_components.powerpal_direct.protocol import Measurement

from .fakes import ADDRESS, BLE_DEVICE, T0, FakeClient, FakePowerpal


async def test_setup_streams_readings_into_entities(hass: HomeAssistant, hass_storage: dict) -> None:
    hass.config.components.add("bluetooth_adapters")
    device = FakePowerpal()
    device.add_minutes(T0 - 600, 10)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Powerpal 00002ba7",
        data={CONF_PAIRING_CODE: 123123},
        options={CONF_PULSES_PER_KWH: 1000.0, CONF_NOTIFICATION_INTERVAL: 1, CONF_POLL_INTERVAL: 0},
    )
    entry.add_to_hass(hass)
    # Totals persisted before a restart, with the last 3 stored minutes not yet counted.
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"total_pulses": 2000, "daily_pulses": 0, "daily_date": None, "watermark": T0 - 240},
    }

    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        bluetooth.async_register_callback.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.05)
        assert entry.state is ConfigEntryState.LOADED

        assert hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "on"
        assert hass.states.get("sensor.powerpal_00002ba7_energy").state == "2.015"
        assert hass.states.get("sensor.powerpal_00002ba7_battery").state == "87"
        assert hass.states.get("sensor.powerpal_00002ba7_power").state == "300.0"

        device.client.notify(Measurement(T0, 10))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.powerpal_00002ba7_energy").state == "2.025"
        assert hass.states.get("sensor.powerpal_00002ba7_power").state == "600.0"

        registry_device = dr.async_get(hass).async_get_device(connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)})
        assert registry_device is not None
        assert registry_device.serial_number == "00002ba7"
        assert registry_device.sw_version == "1.0.4_3"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["total_pulses"] == 2025
