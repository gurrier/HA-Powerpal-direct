"""Taking over from a removed esphome-powerpal_ble gateway."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.powerpal_direct.const import (
    CONF_COST_PER_KWH,
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)
from custom_components.powerpal_direct.gateway import (
    GatewayRates,
    async_detect_gateway_rates,
    find_gateway_entities,
    rates_from_readings,
)

from .fakes import ADDRESS, BLE_DEVICE, T0, FakeClient, FakePowerpal, until

GATEWAY_MAC = "84:CC:A8:52:12:7C"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: object, enable_custom_integrations: None) -> None:
    """The recorder has to start before Home Assistant does, so ask for it first."""


# As the user's own gateway had them: the area prefix came from the device's area.
GATEWAY = {
    ("sensor", "Powerpal Total Energy"): "sensor.powerpal_gateway_powerpal_total_energy",
    ("sensor", "Powerpal Power"): "sensor.powerpal_gateway_powerpal_power",
    ("sensor", "Powerpal Daily Energy"): "sensor.powerpal_gateway_powerpal_daily_energy",
    ("text_sensor", "Powerpal Timestamp"): "sensor.bedroom_powerpal_gateway_powerpal_timestamp",
    ("sensor", "Powerpal Pulses"): "sensor.bedroom_powerpal_gateway_powerpal_pulses",
    ("switch", "Powerpal BLE Connection"): "switch.bedroom_powerpal_gateway_powerpal_ble_connection",
    ("button", "Retrieve Powerpal API Key and Device ID"): (
        "button.bedroom_powerpal_gateway_retrieve_powerpal_api_key_and_device_id"
    ),
    # Nothing to take these over: ESP32-only, or no longer needed.
    ("sensor", "Heap Free"): "sensor.bedroom_powerpal_gateway_heap_free",
}


def register_gateway(hass: HomeAssistant) -> er.EntityRegistry:
    registry = er.async_get(hass)
    for (kind, name), entity_id in GATEWAY.items():
        domain, object_id = entity_id.split(".")
        registry.async_get_or_create(domain, "esphome", f"{GATEWAY_MAC}/0/{kind}/{name}", suggested_object_id=object_id)
    return registry


def remove_gateway(hass: HomeAssistant, registry: er.EntityRegistry) -> None:
    for entity_id in GATEWAY.values():
        registry.async_remove(entity_id)
        hass.states.async_remove(entity_id)


async def test_found_only_once_the_gateway_is_removed(hass: HomeAssistant) -> None:
    registry = register_gateway(hass)
    found = find_gateway_entities(hass)
    assert found.available == {}
    assert found.still_present["energy"] == "sensor.powerpal_gateway_powerpal_total_energy"

    remove_gateway(hass, registry)
    found = find_gateway_entities(hass)
    assert found.still_present == {}
    assert found.available == {
        "energy": "sensor.powerpal_gateway_powerpal_total_energy",
        "power": "sensor.powerpal_gateway_powerpal_power",
        "daily_energy": "sensor.powerpal_gateway_powerpal_daily_energy",
        "last_reading": "sensor.bedroom_powerpal_gateway_powerpal_timestamp",
        "last_pulses": "sensor.bedroom_powerpal_gateway_powerpal_pulses",
        "connection": "switch.bedroom_powerpal_gateway_powerpal_ble_connection",
    }


async def test_older_esphome_unique_ids_are_recognised(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "esphome", "84cca852127c-sensor-powerpal_total_energy")
    assert find_gateway_entities(hass).still_present == {
        "energy": "sensor.esphome_84cca852127c_sensor_powerpal_total_energy"
    }


async def test_takes_over_entity_ids_and_carries_on(hass: HomeAssistant) -> None:
    hass.config.components.add("bluetooth_adapters")
    registry = register_gateway(hass)
    # The gateway's last readings, as the recorder has them.
    # Several readings in the recorder, with the one to carry on from last. Only the
    # newest counts: an earlier version took the oldest, a week-old total in the field.
    for total, daily, stamp in (("19.100", "4.2", T0 - 420), ("19.230", "4.35", T0 - 360), ("19.345", "4.5", T0 - 300)):
        hass.states.async_set("sensor.powerpal_gateway_powerpal_total_energy", total)
        hass.states.async_set("sensor.powerpal_gateway_powerpal_daily_energy", daily)
        hass.states.async_set(
            "sensor.bedroom_powerpal_gateway_powerpal_timestamp", dt_util.utc_from_timestamp(stamp).isoformat()
        )
        await async_wait_recording_done(hass)
    for entity_id in GATEWAY.values():
        hass.states.async_set(entity_id, "unavailable")
    await async_wait_recording_done(hass)
    remove_gateway(hass, registry)
    # Powerpal Direct was set up once before, alongside the gateway, then deleted: Home
    # Assistant remembers those entities and would hand their old IDs back.
    for key, object_id in (("energy", "powerpal_00002ba7_total_energy"), ("power", "powerpal_00002ba7_power")):
        earlier = registry.async_get_or_create("sensor", DOMAIN, f"{ADDRESS}_{key}", suggested_object_id=object_id)
        registry.async_remove(earlier.entity_id)

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
            CONF_COST_PER_KWH: 0.0,
        },
    )
    entry.add_to_hass(hass)
    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
        patch("custom_components.powerpal_direct.coordinator.dt_util.now", return_value=dt_util.now()),
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        bluetooth.async_register_callback.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await until(lambda: hass.states.get("sensor.powerpal_gateway_powerpal_total_energy").state == "19.365")

        ours = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
        assert {
            "sensor.powerpal_gateway_powerpal_total_energy",
            "sensor.powerpal_gateway_powerpal_power",
            "sensor.bedroom_powerpal_gateway_powerpal_timestamp",
            "sensor.bedroom_powerpal_gateway_powerpal_pulses",
            "switch.bedroom_powerpal_gateway_powerpal_ble_connection",
        } <= ours
        # Nothing ended up with a numbered suffix, and ESP32-only entities stayed gone.
        assert not any(entity_id.endswith("_2") for entity_id in ours)
        assert "sensor.bedroom_powerpal_gateway_heap_free" not in ours
        assert "button.bedroom_powerpal_gateway_retrieve_powerpal_api_key_and_device_id" not in ours

        # The total carried on from the gateway's, and catch-up fetched the minutes since
        # its last reading (T0-240 .. T0-60, five pulses each) from the Powerpal's history.
        assert device.history_requests == [(T0 - 299, T0 - 60)]
        assert hass.states.get("sensor.powerpal_gateway_powerpal_total_energy").state == "19.365"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def readings_at(pulses_per_kwh: float, cost_per_kwh: float, pulses: list[int]) -> list[tuple[int, int, float]]:
    """What a gateway with these settings would have published."""
    return [(p, round(p / pulses_per_kwh * 1000), p / pulses_per_kwh * cost_per_kwh) for p in pulses]


@pytest.mark.parametrize("pulses_per_kwh", [1000, 800, 3200, 400, 1600])
def test_rate_from_readings(pulses_per_kwh: int) -> None:
    rates = rates_from_readings(readings_at(pulses_per_kwh, 0.3, [5, 12, 47, 3, 58, 21, 9, 33]))
    assert rates.pulses_per_kwh == pulses_per_kwh
    assert rates.cost_per_kwh == 0.3


def test_an_unusual_rate_is_rounded_not_snapped() -> None:
    assert rates_from_readings(readings_at(1250, 0, [40, 55, 70, 90])).pulses_per_kwh == 1250


def test_too_few_pulses_to_tell() -> None:
    assert rates_from_readings(readings_at(1000, 0, [2, 3, 1])) == GatewayRates()


def test_no_cost_recorded() -> None:
    readings = [(p, wh, None) for p, wh, _ in readings_at(1000, 0, [20, 40])]
    assert rates_from_readings(readings) == GatewayRates(pulses_per_kwh=1000, cost_per_kwh=None)


async def test_rates_detected_from_recorded_history(hass: HomeAssistant, freezer: object) -> None:
    registry = er.async_get(hass)
    for kind, name, object_id in (
        ("sensor", "Powerpal Pulses", "bedroom_powerpal_gateway_powerpal_pulses"),
        ("sensor", "Powerpal Watt Hours", "bedroom_powerpal_gateway_powerpal_watt_hours"),
        ("sensor", "Powerpal Cost", "bedroom_powerpal_gateway_powerpal_cost"),
    ):
        registry.async_get_or_create(
            "sensor", "esphome", f"{GATEWAY_MAC}/0/{kind}/{name}", suggested_object_id=object_id
        )
    for pulses, wh, cost in readings_at(800, 0.25, [16, 40, 24, 56, 8]):
        hass.states.async_set("sensor.bedroom_powerpal_gateway_powerpal_pulses", str(pulses))
        hass.states.async_set("sensor.bedroom_powerpal_gateway_powerpal_watt_hours", str(wh))
        hass.states.async_set("sensor.bedroom_powerpal_gateway_powerpal_cost", str(cost))
        await async_wait_recording_done(hass)
        freezer.tick(60)  # type: ignore[attr-defined]
    rates = await async_detect_gateway_rates(hass, find_gateway_entities(hass))
    assert rates == GatewayRates(pulses_per_kwh=800, cost_per_kwh=0.25)


def test_a_short_history_that_fits_several_rates_takes_the_nearest() -> None:
    # 100 pulses at 1000/kWh is 100 Wh, which no other standard rate reproduces; with
    # only tiny readings both 1000 and 1200 could fit, and the ratio decides.
    assert rates_from_readings([(60, 60, None)]).pulses_per_kwh == 1000


def register_gateway_under(hass: HomeAssistant, esphome_entry: MockConfigEntry, disabled: bool) -> dict[str, str]:
    """Register the gateway's entities to an ESPHome entry. Returns entity ID -> registry ID."""
    registry = er.async_get(hass)
    ids = {}
    for (kind, name), entity_id in GATEWAY.items():
        domain, object_id = entity_id.split(".")
        entry = registry.async_get_or_create(
            domain,
            "esphome",
            f"{GATEWAY_MAC}/0/{kind}/{name}",
            suggested_object_id=object_id,
            config_entry=esphome_entry,
            disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY if disabled else None,
        )
        ids[entity_id] = entry.id
    return ids


async def test_a_live_gateway_is_still_present_and_a_disabled_one_adoptable(hass: HomeAssistant) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    live = MockConfigEntry(domain="esphome", unique_id="live")
    live.add_to_hass(hass)
    register_gateway_under(hass, live, disabled=False)
    found = find_gateway_entities(hass)
    assert found.adoptable == {} and found.available == {}
    assert found.still_present["energy"] == "sensor.powerpal_gateway_powerpal_total_energy"

    await hass.config_entries.async_set_disabled_by(live.entry_id, ConfigEntryDisabler.USER)
    found = find_gateway_entities(hass)
    assert found.still_present == {}
    assert found.adoptable["energy"] == "sensor.powerpal_gateway_powerpal_total_energy"
    assert "sensor.bedroom_powerpal_gateway_heap_free" not in found.adoptable.values()


async def test_adopts_a_disabled_gateways_entity_records(hass: HomeAssistant) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    hass.config.components.add("bluetooth_adapters")
    esphome_entry = MockConfigEntry(domain="esphome", unique_id="84:cc:a8:52:12:7c", title="BLEProxy 52127c")
    esphome_entry.add_to_hass(hass)
    registry_ids = register_gateway_under(hass, esphome_entry, disabled=False)
    # The gateway's last readings, as the recorder has them, then the entry is disabled:
    # the entities are unloaded, but their records stay.
    hass.states.async_set("sensor.powerpal_gateway_powerpal_total_energy", "1821.918")
    hass.states.async_set("sensor.powerpal_gateway_powerpal_daily_energy", "9.437")
    hass.states.async_set(
        "sensor.bedroom_powerpal_gateway_powerpal_timestamp", dt_util.utc_from_timestamp(T0 - 300).isoformat()
    )
    await async_wait_recording_done(hass)
    await hass.config_entries.async_set_disabled_by(esphome_entry.entry_id, ConfigEntryDisabler.USER)
    for entity_id in GATEWAY.values():
        hass.states.async_remove(entity_id)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.powerpal_gateway_powerpal_total_energy").disabled_by is not None

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
            CONF_COST_PER_KWH: 0.0,
        },
    )
    entry.add_to_hass(hass)
    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        bluetooth.async_register_callback.return_value = MagicMock()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await until(lambda: hass.states.get("sensor.powerpal_gateway_powerpal_total_energy").state == "1821.938")

        for entity_id, old_id in registry_ids.items():
            if entity_id in (
                "sensor.bedroom_powerpal_gateway_heap_free",
                "button.bedroom_powerpal_gateway_retrieve_powerpal_api_key_and_device_id",
            ):
                continue  # no counterpart here: stays with ESPHome
            adopted = registry.async_get(entity_id)
            assert adopted is not None, entity_id
            # The same record: that's what keeps the Energy dashboard's cost sensor.
            assert adopted.id == old_id, entity_id
            assert adopted.platform == DOMAIN
            assert adopted.config_entry_id == entry.entry_id
            assert adopted.disabled_by is None
            assert hass.states.get(entity_id) is not None, entity_id
        assert registry.async_get("sensor.bedroom_powerpal_gateway_heap_free").platform == "esphome"
        # Carried on from the gateway: its total plus the two minutes catch-up recovered.
        assert device.history_requests == [(T0 - 299, T0 - 60)]

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
