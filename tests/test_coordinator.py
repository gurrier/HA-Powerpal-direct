"""Catching up from the Powerpal's stored history without double counting."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import time
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

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
from custom_components.powerpal_direct.protocol import HistoryRange, Measurement

from .fakes import ADDRESS, BLE_DEVICE, PAIRING_CODE, T0, FakeClient, FakePowerpal, until


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
    await until(lambda: coordinator.data.connected)
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
        await until(lambda: start_reauth.called)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    start_reauth.assert_called_once()
    assert "Pairing code" in (coordinator.data.last_error or "")


async def test_run_loop_raises_repair_issue_when_bonding_fails(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    issue_id = f"bond_failed_{coordinator.config_entry.entry_id}"
    with (
        patch("custom_components.powerpal_direct.coordinator.bluetooth") as bluetooth,
        patch.object(FakeClient, "connect", side_effect=PowerpalBondError("org.bluez.Error.AuthenticationFailed")),
        patch.object(coordinator.config_entry, "async_start_reauth") as start_reauth,
    ):
        bluetooth.async_ble_device_from_address.return_value = BLE_DEVICE
        task = asyncio.create_task(coordinator._async_run())
        await until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The code isn't the problem, so don't ask for it again.
    start_reauth.assert_not_called()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # A later session that authenticates clears it.
    coordinator.energy.state = EnergyState(watermark=device.history_range.newest)
    await coordinator._async_session(BLE_DEVICE)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_empty_history_lets_the_first_live_reading_start_the_count(
    hass: HomeAssistant, device: FakePowerpal
) -> None:
    # A Powerpal that has just reset reports (0, 0) for its stored range.
    device.records.clear()
    with patch.object(FakePowerpal, "history_range", HistoryRange(oldest=0, newest=0)):
        coordinator = make_coordinator(hass)
        session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
        await until(lambda: coordinator.data.connected)
        assert coordinator.energy.watermark is None
        assert coordinator.data.history_oldest is None

        device.client.notify(Measurement(T0, 4))
        device.client.notify(Measurement(T0 + 60, 5))
        await device.client.disconnect()
        await asyncio.wait_for(session, 1)

    assert device.history_requests == []
    assert coordinator.energy.state.total_pulses == 9
    assert coordinator.energy.watermark == T0 + 60


async def test_clock_left_alone_when_the_powerpal_has_history(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass, poll_interval=15)
    await coordinator._async_session(BLE_DEVICE)
    assert device.clock_writes == []


async def test_clock_set_on_connect_after_a_reset(hass: HomeAssistant, device: FakePowerpal) -> None:
    device.records.clear()
    with patch.object(FakePowerpal, "history_range", HistoryRange(oldest=0, newest=0)):
        coordinator = make_coordinator(hass, poll_interval=15)
        await coordinator._async_session(BLE_DEVICE)
    assert len(device.clock_writes) == 1


async def test_clock_set_when_no_reading_arrives(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.energy.state = EnergyState(watermark=device.history_range.newest)
    with patch("custom_components.powerpal_direct.coordinator.CLOCK_NO_READING_GRACE_S", -59.9):
        session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
        await until(lambda: coordinator.data.connected)
        await asyncio.sleep(0.2)
        coordinator._catch_up_requested.set()  # wakes the loop to check
        await until(lambda: len(device.clock_writes) == 1)
        await device.client.disconnect()
        await asyncio.wait_for(session, 1)


async def test_clock_corrected_only_when_it_drifts(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    newest = device.history_range.newest
    coordinator.energy.state = EnergyState(watermark=newest)
    session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
    await until(lambda: coordinator.data.connected)

    # Readings arriving on time: about 60 s after their own timestamp.
    with patch("custom_components.powerpal_direct.coordinator.time.time") as now:
        for minute in range(1, 7):
            now.return_value = newest + minute * 60 + 60 + 2
            device.client.notify(Measurement(newest + minute * 60, 1))
        assert coordinator.data.clock_offset == 2
        assert not coordinator._clock_correction_due

        # The Powerpal's clock falls 2 minutes behind.
        for minute in range(7, 13):
            now.return_value = newest + minute * 60 + 60 + 120
            device.client.notify(Measurement(newest + minute * 60, 1))
        assert coordinator.data.clock_offset == 120
        assert coordinator._clock_correction_due

    coordinator._catch_up_requested.set()
    await until(lambda: len(device.clock_writes) == 1)
    assert not coordinator._clock_correction_due
    await device.client.disconnect()
    await asyncio.wait_for(session, 1)


async def test_unplanned_drop_is_reported_as_last_stall(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.energy.state = EnergyState(watermark=device.history_range.newest - 180)
    session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
    await until(lambda: coordinator.data.connected)
    # The link drops on its own, a while after connecting: not requested, not the switch.
    coordinator._connected_at = time.monotonic() - 100
    coordinator.data.last_error = None
    await device.client.disconnect()
    await asyncio.wait_for(session, 1)
    coordinator._note_drop()
    coordinator._set_connected(False)

    coordinator.energy.state.watermark = device.history_range.newest - 120
    session = asyncio.create_task(coordinator._async_session(BLE_DEVICE))
    await until(lambda: coordinator.data.last_stall is not None)
    assert "the link dropped" in coordinator.data.last_stall
    assert "recovered 2 of 2 readings" in coordinator.data.last_stall
    await device.client.disconnect()
    await asyncio.wait_for(session, 1)


async def test_switching_off_is_not_a_stall(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.data.connected = True
    await coordinator.async_set_connection_enabled(False)
    coordinator._note_drop()
    assert coordinator._drop_reason is None


async def test_advertisement_rate_from_how_often_it_is_heard(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator._async_on_advertisement(MagicMock(rssi=-61), MagicMock())
    assert coordinator.data.advertisement_rssi == -61

    base = "custom_components.powerpal_direct.coordinator.bluetooth"
    heard = MagicMock(time=time.monotonic() - 5)
    with (
        patch(f"{base}.async_last_service_info", return_value=heard),
        patch(f"{base}.async_get_learned_advertising_interval", return_value=2.0),
    ):
        coordinator._async_sample_advertisement_rate(dt_util.now())
        assert coordinator.data.advertisement_rate == 30
    with patch(f"{base}.async_last_service_info", return_value=MagicMock(time=time.monotonic() - 120)):
        coordinator._async_sample_advertisement_rate(dt_util.now())
        assert coordinator.data.advertisement_rate == 0  # not heard: the link is up
    with (
        patch(f"{base}.async_last_service_info", return_value=heard),
        patch(f"{base}.async_get_learned_advertising_interval", return_value=None),
    ):
        coordinator._async_sample_advertisement_rate(dt_util.now())
        assert coordinator.data.advertisement_rate is None  # heard, interval not learned yet


async def test_a_burst_of_late_readings_does_not_skew_the_clock(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    newest = device.history_range.newest
    coordinator.energy.state = EnergyState(watermark=newest)
    with patch("custom_components.powerpal_direct.coordinator.time.time", return_value=newest + 20 * 60):
        # Ten minutes of stored readings, all arriving at once on reconnecting.
        for minute in range(1, 11):
            coordinator._note_clock_offset(Measurement(newest + minute * 60, 1))
    assert coordinator.data.clock_offset is None
    assert not coordinator._clock_correction_due


async def test_a_drop_just_after_connecting_is_not_a_stall(hass: HomeAssistant, device: FakePowerpal) -> None:
    coordinator = make_coordinator(hass)
    coordinator.data.connected = True
    coordinator._connected_at = time.monotonic() - 3
    coordinator._note_drop()
    assert coordinator._drop_reason is None
    coordinator._connected_at = time.monotonic() - 100
    coordinator._note_drop()
    assert coordinator._drop_reason == "the link dropped"


async def test_connected_via_follows_the_slot_holding_the_connection(hass: HomeAssistant, device: FakePowerpal) -> None:
    from types import SimpleNamespace

    allocations = [SimpleNamespace(source="18:1D:EA:F5:66:6B", allocated=[ADDRESS.lower()])]
    manager = MagicMock()
    manager.async_current_allocations.side_effect = lambda: allocations
    scanners = {
        "18:1D:EA:F5:66:6B": SimpleNamespace(name="hci0 (18:1D:EA:F5:66:6B)"),
        "24:EC:4A:B0:B2:0A": SimpleNamespace(name="athom-smart-plug-v3-dishwasher (24:EC:4A:B0:B2:0A)"),
    }
    coordinator = make_coordinator(hass)
    with (
        patch("custom_components.powerpal_direct.coordinator._bluetooth_manager", return_value=manager),
        patch(
            "custom_components.powerpal_direct.coordinator.bluetooth.async_scanner_by_source",
            side_effect=lambda hass, source: scanners.get(source),
        ),
    ):
        coordinator._set_connected(True)
        assert coordinator.data.connected_via == "hci0 (18:1D:EA:F5:66:6B)"

        # A proxy took it: its slot report arrives through the allocation callback.
        allocations = [
            SimpleNamespace(source="18:1D:EA:F5:66:6B", allocated=[]),
            SimpleNamespace(source="24:EC:4A:B0:B2:0A", allocated=[ADDRESS]),
        ]
        coordinator._async_on_allocations(allocations[1])
        assert coordinator.data.connected_via == "athom-smart-plug-v3-dishwasher (24:EC:4A:B0:B2:0A)"

        coordinator._set_connected(False)
        assert coordinator.data.connected_via is None


async def test_new_install_imports_stored_history_into_hourly_only(hass: HomeAssistant, device: FakePowerpal) -> None:
    from custom_components.powerpal_direct.hourly import HourlyEnergyStatistic

    written: list[list[tuple[int, float]]] = []
    coordinator = make_coordinator(hass, poll_interval=15)
    with (
        patch.object(HourlyEnergyStatistic, "available", True),
        patch.object(HourlyEnergyStatistic, "_write", lambda self, rows: written.append(rows)),
    ):
        await coordinator._async_session(BLE_DEVICE)

    # The ten stored minutes (5 pulses each) went into the hourly statistic...
    assert device.history_requests == [(T0 - 600, T0 - 60)]
    assert sum(coordinator.hourly.ledger.hours.values()) == 50
    assert written
    # ...but the new total still starts from now.
    assert coordinator.energy.state.total_pulses == 0
    assert coordinator.energy.watermark == T0 - 60
