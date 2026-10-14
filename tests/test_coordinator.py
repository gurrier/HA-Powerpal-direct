"""Catching up from the Powerpal's stored history without double counting."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.powerpal_direct.client import PowerpalBondError
from custom_components.powerpal_direct.const import (
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)
from custom_components.powerpal_direct.coordinator import PowerpalCoordinator
from custom_components.powerpal_direct.energy import EnergyState
from custom_components.powerpal_direct.protocol import Measurement

from .fakes import ADDRESS, BLE_DEVICE, PAIRING_CODE, T0, FakeClient, FakePowerpal


@pytest.fixture
def device() -> Generator[FakePowerpal]:
    device = FakePowerpal()
    device.add_minutes(T0 - 600, 10)
    with (
        patch.object(FakeClient, "device", device, create=True),
        patch("custom_components.powerpal_direct.coordinator.PowerpalClient", FakeClient),
    ):
        yield device


def make_coordinator(
    hass: HomeAssistant, poll_interval: int = 0, pairing_code: int = PAIRING_CODE
) -> PowerpalCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Powerpal 00002ba7",
        data={CONF_PAIRING_CODE: pairing_code},
        options={CONF_PULSES_PER_KWH: 1000.0, CONF_NOTIFICATION_INTERVAL: 1, CONF_POLL_INTERVAL: poll_interval},
    )
    entry.add_to_hass(hass)
    return PowerpalCoordinator(hass, entry)


async def test_first_connection_starts_counting_from_now(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    await coordinator._async_session(BLE_DEVICE)

    assert device.history_requests == []
    assert coordinator.energy.state.total_pulses == 0
    assert coordinator.energy.watermark == device.history_range.newest
    assert coordinator.data.identity is not None and coordinator.data.identity.device_id == "00002ba7"
    assert coordinator.data.battery == 87


async def test_catch_up_counts_only_records_after_watermark(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    coordinator.energy.state = EnergyState(total_pulses=1000, watermark=T0 - 300)
    await coordinator._async_session(BLE_DEVICE)

    assert device.history_requests == [(T0 - 299, T0 - 60)]
    # Records at T0-240 .. T0-60: four of them, five pulses each.
    assert coordinator.energy.state.total_pulses == 1020
    assert coordinator.energy.watermark == T0 - 60
    result = coordinator.data.last_catch_up
    assert result is not None and (result.expected, result.received, result.counted) == (4, 4, 4)
    # 5 pulses in a minute at 1000 pulses/kWh is 300 W.
    assert coordinator.data.power_w == 300.0
    assert coordinator.total_kwh == pytest.approx(1.02)

    # Visiting again with nothing new stored requests nothing and counts nothing.
    await coordinator._async_session(BLE_DEVICE)
    assert len(device.history_requests) == 1
    assert coordinator.energy.state.total_pulses == 1020


async def test_live_reading_during_catch_up_is_counted_after_it(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    coordinator.energy.state = EnergyState(watermark=T0 - 300)
    device.live_during_history = [Measurement(T0, 7)]
    await coordinator._async_session(BLE_DEVICE)

    assert coordinator.energy.state.total_pulses == 4 * 5 + 7
    assert coordinator.energy.watermark == T0


async def test_outage_longer_than_stored_history(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    coordinator.energy.state = EnergyState(watermark=T0 - 1200)
    await coordinator._async_session(BLE_DEVICE)

    result = coordinator.data.last_catch_up
    assert result is not None
    # Requested from T0-1199; the oldest held is T0-600, so 9 whole minutes are gone.
    assert result.unrecoverable_minutes == 9
    assert (result.expected, result.counted) == (10, 10)
    assert coordinator.energy.state.total_pulses == 50


async def test_gap_on_live_connection_is_backfilled(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.energy.state = EnergyState(watermark=T0 - 600)
    client = FakeClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    await client.subscribe(coordinator._on_measurement, MagicMock())
    coordinator._client = client

    # Three minutes of live readings went missing; the next one shows the gap.
    device.add_minutes(T0, 1, pulses=6)
    client.notify(Measurement(T0, 6))
    assert coordinator._catch_up_requested.is_set()
    assert coordinator.energy.state.total_pulses == 0

    await coordinator._async_catch_up(client, coordinator._history_window)
    assert device.history_requests == [(T0 - 599, T0 - 1)]
    assert coordinator.energy.state.total_pulses == 9 * 5 + 6
    assert coordinator.energy.watermark == T0


async def test_persistent_session_streams_until_disconnect(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.energy.state = EnergyState(watermark=device.history_range.newest)
    session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
    await asyncio.sleep(0.05)
    assert coordinator.data.connected
    assert device.batch_size == 1

    newest = device.history_range.newest
    device.client.notify(Measurement(newest + 60, 2))
    device.client.notify(Measurement(newest + 120, 3))
    assert coordinator.energy.state.total_pulses == 5
    assert coordinator.data.power_w == 180.0
    assert coordinator.power_available

    await device.client.disconnect()
    await asyncio.wait_for(session, 1)


async def test_run_loop_reauths_on_rejected_code(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, pairing_code=111111)
    with (
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
        patch.object(coordinator.config_entry, "async_start_reauth") as start_reauth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        task = asyncio.create_task(coordinator._async_run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    start_reauth.assert_called_once()
    assert "Pairing code" in (coordinator.data.last_error or "")


async def test_run_loop_raises_repair_issue_when_bonding_fails(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    with (
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
        patch.object(FakeClient, "connect", side_effect=PowerpalBondError("org.bluez.Error.AuthenticationFailed")),
        patch.object(coordinator.config_entry, "async_start_reauth") as start_reauth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        task = asyncio.create_task(coordinator._async_run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The code isn't the problem, so don't ask for it again.
    start_reauth.assert_not_called()
    issue_id = f"bond_failed_{coordinator.config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # A later session that authenticates clears it.
    coordinator.energy.state = EnergyState(watermark=device.history_range.newest)
    await coordinator._async_session(BLE_DEVICE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
