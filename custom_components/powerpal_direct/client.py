"""One BLE connection to a Powerpal: bond, authenticate, read and subscribe."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from . import protocol
from .bluez_agent import pairing_agent
from .protocol import HistoryRange, Measurement

_LOGGER = logging.getLogger(__name__)

GATT_TIMEOUT_S = 15
# The reference scripts leave this long between the unacknowledged pairing code write
# and the read that checks it took.
AUTH_SETTLE_S = 1.0

# Device Information Service, read once for the device registry.
CHAR_MODEL_NUMBER = "00002a24-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_HARDWARE_REVISION = "00002a27-0000-1000-8000-00805f9b34fb"


class PowerpalAuthError(Exception):
    """The Powerpal didn't accept the pairing code."""


class PowerpalBondError(Exception):
    """This host couldn't bond with the Powerpal, so it can't read anything at all."""


# Errors that mean the link isn't bonded (or bonding failed), as opposed to a wrong
# pairing code. Matched on text: each backend raises its own error types.
_BOND_FAILURE_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "authenticationfailed",
    "authenticationrejected",
    "authenticationcanceled",
    "authenticationtimeout",
    "pairing failed",
)


def is_bond_failure(err: BaseException) -> bool:
    """Whether an error, or anything it was raised from, is a bonding failure."""
    seen: BaseException | None = err
    while seen is not None:
        if any(marker in str(seen).lower() for marker in _BOND_FAILURE_MARKERS):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


@dataclass(slots=True)
class DeviceIdentity:
    """What the Powerpal says about itself once authenticated."""

    device_id: str
    api_key: str
    model: str | None = None
    firmware: str | None = None
    hardware: str | None = None


class PowerpalClient:
    """A single connection. Make a new one to reconnect."""

    def __init__(
        self,
        ble_device: BLEDevice,
        pairing_code: int,
        ble_device_callback: Callable[[], BLEDevice] | None = None,
        disconnected_callback: Callable[[], None] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._pairing_code = pairing_code
        self._pairing_payload = protocol.encode_pairing_code(pairing_code)
        self._ble_device_callback = ble_device_callback
        self._disconnected_callback = disconnected_callback
        self._client: BleakClient | None = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise BleakError("Not connected to the Powerpal")
        return self._client

    async def connect(self) -> None:
        """Connect, bonding first if this host hasn't bonded with the Powerpal yet.

        Bonding is mandatory: every read fails with Insufficient Authentication until
        it's done. The ceremony is Just Works, so there's nothing to enter.
        """

        def _on_disconnect(_client: BleakClient) -> None:
            if self._disconnected_callback is not None:
                self._disconnected_callback()

        try:
            async with pairing_agent(self._ble_device.address, self._pairing_code):
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._ble_device,
                    self._ble_device.name or self._ble_device.address,
                    disconnected_callback=_on_disconnect,
                    ble_device_callback=self._ble_device_callback,
                    pair=True,
                )
        except NotImplementedError as err:
            # An ESPHome Bluetooth proxy on firmware too old to pair.
            raise PowerpalBondError(str(err)) from err
        except BleakError as err:
            if is_bond_failure(err):
                raise PowerpalBondError(str(err)) from err
            raise

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except BleakError as err:
                _LOGGER.debug("%s: Error disconnecting: %s", self._ble_device.address, err)

    async def authenticate(self) -> str:
        """Write the pairing code, and return the API key read back afterwards.

        The API key reads back whether or not the code took, so it only proves the link
        is bonded. A wrong code can't be told apart here: the Powerpal simply never sends
        readings, which the coordinator reports after a few silent connections.
        """
        try:
            return await self._try_authenticate()
        except BleakError as err:
            # pair=True at connect should already have bonded, but not every backend
            # does it there. Bond explicitly and try once more.
            _LOGGER.debug("%s: Pairing code not accepted (%s); bonding and retrying", self._ble_device.address, err)
        client = self._require_client()
        try:
            async with (
                pairing_agent(self._ble_device.address, self._pairing_code),
                asyncio.timeout(GATT_TIMEOUT_S * 2),
            ):
                await client.pair()
        except (BleakError, NotImplementedError) as err:
            raise PowerpalBondError(f"Bonding failed: {err}") from err
        try:
            return await self._try_authenticate()
        except BleakError as err:
            if is_bond_failure(err):
                raise PowerpalBondError(f"Still not bonded after pairing: {err}") from err
            if not self.is_connected:
                # The link went away mid-way: nothing to do with the code. Let the
                # connection loop retry, rather than asking for the code again.
                raise
            raise PowerpalAuthError(f"Pairing code not accepted: {err}") from err

    async def _try_authenticate(self) -> str:
        await self._write_pairing_code()
        await asyncio.sleep(AUTH_SETTLE_S)
        raw = await self._read(protocol.CHAR_API_KEY)
        try:
            return protocol.decode_api_key(raw)
        except protocol.PowerpalProtocolError as err:
            raise PowerpalAuthError(f"Unexpected reply checking the pairing code: {err}") from err

    async def _write_pairing_code(self) -> None:
        """Write the code with response, as the ESP32 gateway does.

        The characteristic doesn't support write-without-response, so BlueZ's write
        command is silently discarded by the Powerpal (seen on a real one: no error, but
        nothing unlocked). Windows appears to turn it into a write request, which is why
        the reference scripts got away with it. Without response is kept only as a
        fallback for a host that can't do the request.
        """
        client = self._require_client()
        try:
            async with asyncio.timeout(GATT_TIMEOUT_S):
                await client.write_gatt_char(protocol.CHAR_PAIRING_CODE, self._pairing_payload, response=True)
            return
        except BleakError as err:
            if is_bond_failure(err):
                raise
            _LOGGER.debug("%s: Pairing code write request failed (%s); trying a write command", self.address, err)
        except TimeoutError:
            _LOGGER.debug("%s: Pairing code write request timed out; trying a write command", self.address)
        async with asyncio.timeout(GATT_TIMEOUT_S):
            await client.write_gatt_char(protocol.CHAR_PAIRING_CODE, self._pairing_payload, response=False)

    @property
    def address(self) -> str:
        return self._ble_device.address

    async def _read(self, char: str) -> bytes:
        client = self._require_client()
        async with asyncio.timeout(GATT_TIMEOUT_S):
            return bytes(await client.read_gatt_char(char))

    async def _read_optional(self, char: str) -> bytes | None:
        """Read a characteristic that not every firmware may have."""
        client = self._require_client()
        if client.services.get_characteristic(char) is None:
            return None
        try:
            return await self._read(char)
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: Couldn't read %s: %s", self._ble_device.address, char, err)
            return None

    async def read_identity(self, api_key: str) -> DeviceIdentity:
        device_id = protocol.decode_device_id(await self._read(protocol.CHAR_SERIAL))
        identity = DeviceIdentity(device_id=device_id, api_key=api_key)
        for char, attr in (
            (CHAR_MODEL_NUMBER, "model"),
            (CHAR_FIRMWARE_REVISION, "firmware"),
            (CHAR_HARDWARE_REVISION, "hardware"),
        ):
            if (raw := await self._read_optional(char)) is not None:
                text = raw.decode("utf-8", errors="replace").strip("\x00 ")
                setattr(identity, attr, text or None)
        return identity

    async def read_battery(self) -> int | None:
        raw = await self._read_optional(protocol.CHAR_BATTERY_LEVEL)
        return None if raw is None else protocol.decode_single_byte(raw)

    async def read_led_sensitivity(self) -> int | None:
        raw = await self._read_optional(protocol.CHAR_LED_SENSITIVITY)
        return None if raw is None else protocol.decode_single_byte(raw)

    async def read_history_range(self) -> HistoryRange:
        return protocol.decode_history_range(await self._read(protocol.CHAR_FIRST_REC))

    async def ensure_reading_batch_size(self, minutes: int) -> None:
        """Set how often live readings are sent, writing only if it differs.

        Lowering it makes the Powerpal send everything it buffered at the next reading,
        which the watermark makes harmless.
        """
        current = protocol.decode_uint32(await self._read(protocol.CHAR_READING_BATCH_SIZE))
        if current & 0xFF == minutes:
            return
        _LOGGER.info(
            "%s: Changing reading batch size from %s to %s minutes", self._ble_device.address, current & 0xFF, minutes
        )
        client = self._require_client()
        async with asyncio.timeout(GATT_TIMEOUT_S):
            await client.write_gatt_char(
                protocol.CHAR_READING_BATCH_SIZE, protocol.encode_uint32(minutes), response=True
            )

    async def subscribe(
        self,
        on_measurement: Callable[[Measurement], None],
        on_battery: Callable[[int], None],
    ) -> None:
        """Subscribe to live readings (which also carry history replies) and battery level."""
        client = self._require_client()
        address = self._ble_device.address

        def _measurement(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            try:
                measurement = protocol.decode_measurement(data)
            except protocol.PowerpalProtocolError as err:
                _LOGGER.warning("%s: Ignoring measurement: %s", address, err)
                return
            on_measurement(measurement)

        def _battery(_char: BleakGATTCharacteristic, data: bytearray) -> None:
            try:
                on_battery(protocol.decode_single_byte(data))
            except protocol.PowerpalProtocolError as err:
                _LOGGER.debug("%s: Ignoring battery notification: %s", address, err)

        async with asyncio.timeout(GATT_TIMEOUT_S):
            await client.start_notify(protocol.CHAR_MEASUREMENT, _measurement)
        if client.services.get_characteristic(protocol.CHAR_BATTERY_LEVEL) is not None:
            try:
                async with asyncio.timeout(GATT_TIMEOUT_S):
                    await client.start_notify(protocol.CHAR_BATTERY_LEVEL, _battery)
            except (BleakError, TimeoutError) as err:
                # Battery is re-read periodically anyway.
                _LOGGER.debug("%s: Battery notifications unavailable: %s", address, err)

    async def request_history(self, start: int, end: int) -> None:
        """Ask for stored records in [start, end]; they arrive as measurement notifications."""
        client = self._require_client()
        async with asyncio.timeout(GATT_TIMEOUT_S):
            await client.write_gatt_char(
                protocol.CHAR_MEASUREMENT_ACCESS, protocol.encode_history_request(start, end), response=True
            )

    async def set_time(self, timestamp: int) -> None:
        client = self._require_client()
        async with asyncio.timeout(GATT_TIMEOUT_S):
            await client.write_gatt_char(protocol.CHAR_TIME, protocol.encode_uint32(timestamp), response=True)
