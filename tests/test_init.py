"""Setting up the entry end to end, against a fake Powerpal."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from custom_components.powerpal_direct.const import (
    CONF_COST_PER_KWH,
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)
from custom_components.powerpal_direct.protocol import Measurement

from .fakes import ADDRESS, API_KEY, BLE_DEVICE, DEVICE_ID, T0, FakeClient, FakePowerpal, until


async def test_setup_streams_readings_into_entities(
    hass: HomeAssistant, hass_storage: dict, aioclient_mock: AiohttpClientMocker
) -> None:
    hass.config.components.add("bluetooth_adapters")
    device = FakePowerpal()
    device.add_minutes(T0 - 600, 10)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Powerpal 00002ba7",
        data={CONF_PAIRING_CODE: 123123},
        options={
            CONF_PULSES_PER_KWH: 1000.0,
            CONF_NOTIFICATION_INTERVAL: 1,
            CONF_POLL_INTERVAL: 0,
            CONF_COST_PER_KWH: 0.3,
        },
    )
    entry.add_to_hass(hass)
    # Totals persisted before a restart, with the last 3 stored minutes not yet counted.
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"total_pulses": 2000, "daily_pulses": 0, "daily_date": None, "watermark": T0 - 240},
    }

    # Every value Total Energy is ever written with, from the very first.
    written: list[str] = []

    @callback
    def _record(event: Event) -> None:
        if event.data["entity_id"] == "sensor.powerpal_00002ba7_total_energy" and event.data["new_state"]:
            written.append(event.data["new_state"].state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        bluetooth.async_register_callback.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await until(lambda: hass.states.get("sensor.powerpal_00002ba7_total_energy").state == "2.015")
        # The saved total is there from the first write: no dip to 0 that statistics
        # would read as a meter reset, or the cost sensor would charge as consumption.
        assert written[0] == "2.0"
        assert entry.state is ConfigEntryState.LOADED

        assert hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "on"
        assert hass.states.get("sensor.powerpal_00002ba7_total_energy").state == "2.015"
        assert hass.states.get("sensor.powerpal_00002ba7_battery").state == "87"
        assert hass.states.get("sensor.powerpal_00002ba7_power").state == "300.0"

        device.client.notify(Measurement(T0, 10))
        await hass.async_block_till_done()
        assert hass.states.get("sensor.powerpal_00002ba7_total_energy").state == "2.025"
        assert hass.states.get("sensor.powerpal_00002ba7_power").state == "600.0"
        # The gateway's per-reading sensors, which the Powerpal cloud upload uses.
        assert hass.states.get("sensor.powerpal_00002ba7_pulses").state == "10"
        assert hass.states.get("sensor.powerpal_00002ba7_watt_hours").state == "10"
        cost = hass.states.get("sensor.powerpal_00002ba7_cost")
        assert float(cost.state) == 0.003
        assert cost.attributes["unit_of_measurement"] == hass.config.currency

        # Read from the Powerpal on connect. The API key is never exposed as an entity.
        assert hass.states.get("sensor.powerpal_00002ba7_device_id").state == "00002ba7"
        assert not [entity_id for entity_id in hass.states.async_entity_ids() if "api_key" in entity_id]
        # Cloud upload is off until turned on; then readings go up with the Powerpal's own
        # device ID and API key.
        assert hass.states.get("switch.powerpal_00002ba7_cloud_upload").state == "off"
        aioclient_mock.post(f"https://readings.powerpal.net/api/v1/meter_reading/{DEVICE_ID}", status=200)
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.powerpal_00002ba7_cloud_upload"}, blocking=True
        )
        device.client.notify(Measurement(T0 + 60, 3))
        await hass.async_block_till_done()
        assert aioclient_mock.call_count == 1
        assert aioclient_mock.mock_calls[0][2] == [
            {"cost": 0.0009, "is_peak": False, "pulses": 3, "timestamp": T0 + 60, "watt_hours": 3}
        ]
        assert aioclient_mock.mock_calls[0][3]["authorization"] == API_KEY

        # The connection switch lets go of the Powerpal, and takes it back.
        switch = "switch.powerpal_00002ba7_ble_connection"
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
        await until(lambda: hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "off")
        assert hass.states.get(switch).state == "off"
        assert hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "off"
        assert not device.client.is_connected
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        await until(lambda: hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "on")
        assert hass.states.get(switch).state == "on"
        assert hass.states.get("binary_sensor.powerpal_00002ba7_connectivity").state == "on"

        registry_device = dr.async_get(hass).async_get_device(connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)})
        assert registry_device is not None
        assert registry_device.serial_number == "00002ba7"
        assert registry_device.sw_version == "1.0.4_3"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["total_pulses"] == 2028


async def test_connection_switch_left_off_stays_off_after_restart(hass: HomeAssistant) -> None:
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    from homeassistant.core import State

    hass.config.components.add("bluetooth_adapters")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Powerpal 00002ba7",
        data={CONF_PAIRING_CODE: 123123},
        options={CONF_PULSES_PER_KWH: 1000.0, CONF_NOTIFICATION_INTERVAL: 1, CONF_POLL_INTERVAL: 0},
    )
    entry.add_to_hass(hass)
    mock_restore_cache(hass, [State("switch.powerpal_00002ba7_ble_connection", "off")])
    device = FakePowerpal()
    device.add_minutes(T0 - 600, 10)
    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        bluetooth.async_register_callback.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.1)
        assert hass.states.get("switch.powerpal_00002ba7_ble_connection").state == "off"
        assert device.client is None  # never connected
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
