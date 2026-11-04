"""A stand-in Powerpal that behaves like the real one over BLE, minus the radio."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from custom_components.powerpal_direct.client import DeviceIdentity, PowerpalAuthError
from custom_components.powerpal_direct.protocol import RECORD_INTERVAL_S, HistoryRange, Measurement

ADDRESS = "DF:5C:55:00:00:01"
PAIRING_CODE = 123123
DEVICE_ID = "00002ba7"
API_KEY = "95210d4e-894f-42b7-b882-4b94df7daa34"
# A minute boundary two hours ago. Relative to now, because the hourly statistic keeps
# only the last 31 days by the hour; a fixed date would age out of that window.
T0 = (int(time.time()) // 60) * 60 - 2 * 3600

BLE_DEVICE = BLEDevice(ADDRESS, "Powerpal 00002ba7", None)


def service_info() -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="Powerpal 00002ba7",
        address=ADDRESS,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=["0000fcd7-0000-1000-8000-00805f9b34fb"],
        source="local",
        device=BLE_DEVICE,
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


class FakePowerpal:
    """The device: a store of per-minute records and one connection slot."""

    def __init__(self, records: dict[int, int] | None = None) -> None:
        self.records: dict[int, int] = dict(records or {})
        self.batch_size = 15
        self.history_requests: list[tuple[int, int]] = []
        self.clock_writes: list[int] = []
        # Sent ahead of the history reply, as if a live reading crossed it in flight.
        self.live_during_history: list[Measurement] = []
        self.client: FakeClient | None = None

    def add_minutes(self, start: int, count: int, pulses: int = 5) -> None:
        for i in range(count):
            self.records[start + i * RECORD_INTERVAL_S] = pulses

    @property
    def history_range(self) -> HistoryRange:
        return HistoryRange(oldest=min(self.records), newest=max(self.records))


class FakeClient:
    """Drop-in for PowerpalClient, talking to a FakePowerpal."""

    device: FakePowerpal

    def __init__(
        self,
        ble_device: BLEDevice,
        pairing_code: int,
        ble_device_callback: Callable[[], BLEDevice] | None = None,
        disconnected_callback: Callable[[], None] | None = None,
    ) -> None:
        self._code = pairing_code
        self._disconnected_callback = disconnected_callback
        self._connected = False
        self._on_measurement: Callable[[Measurement], None] | None = None
        self.device.client = self

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            if self._disconnected_callback is not None:
                self._disconnected_callback()

    async def authenticate(self) -> str:
        if self._code != PAIRING_CODE:
            raise PowerpalAuthError("Pairing code not accepted")
        return API_KEY

    async def read_identity(self, api_key: str) -> DeviceIdentity:
        return DeviceIdentity(device_id=DEVICE_ID, api_key=api_key, firmware="1.0.4_3")

    async def read_battery(self) -> int:
        return 87

    async def read_led_sensitivity(self) -> int:
        return 9

    async def read_history_range(self) -> HistoryRange:
        if not self._connected:
            raise BleakError("Not connected")
        return self.device.history_range

    async def ensure_reading_batch_size(self, minutes: int) -> None:
        self.device.batch_size = minutes

    async def subscribe(self, on_measurement, on_battery) -> None:
        self._on_measurement = on_measurement

    async def request_history(self, start: int, end: int) -> None:
        self.device.history_requests.append((start, end))
        loop = asyncio.get_running_loop()
        for live in self.device.live_during_history:
            loop.call_soon(self.notify, live)
        for timestamp in sorted(self.device.records):
            if start <= timestamp <= end:
                loop.call_soon(self.notify, Measurement(timestamp, self.device.records[timestamp]))

    async def set_time(self, timestamp: int) -> None:
        self.device.clock_writes.append(timestamp)

    def notify(self, measurement: Measurement) -> None:
        """Send a measurement notification, as the Powerpal does every reading."""
        assert self._on_measurement is not None
        self._on_measurement(measurement)


async def until(condition: Callable[[], object], timeout: float = 2.0) -> None:
    """Wait for background work to reach a state, rather than sleeping a fixed time.

    A fixed sleep is flaky on a busy machine; this waits only as long as needed.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("Timed out waiting for the condition")
        await asyncio.sleep(0.01)
