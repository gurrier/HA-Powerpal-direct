# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Powerpal Direct wire format.

Everything here is pure: no Home Assistant and no bleak, so it can be tested on its own.
Sources: https://github.com/gurrier/esphome-powerpal_ble for the live readings, and
https://github.com/gurrier/Powerpal-protocol for bonding, authentication and history
(verified on firmware 1.0.4_3). All multi-byte integers are little endian.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
import uuid

SERVICE_UUID = "59daabcd-12f4-25a6-7d4f-55961dce4205"
# What the Powerpal actually advertises. It never advertises SERVICE_UUID, so a scan
# filter on that would never match.
ADVERTISED_SERVICE_UUID = "0000fcd7-0000-1000-8000-00805f9b34fb"
LOCAL_NAME_PREFIX = "Powerpal"


def _char(short: str) -> str:
    # The short codes are literal text: 0009 is followed by 0010, not 000a.
    return f"59da{short}-12f4-25a6-7d4f-55961dce4205"


# Live readings and history replies both arrive here as notifications, one record each.
CHAR_MEASUREMENT = _char("0001")
# Write a (start, end) window to have that stored history replayed on CHAR_MEASUREMENT.
CHAR_MEASUREMENT_ACCESS = _char("0002")
# Not a clock: it reads back the last value written, so it can only set the time.
CHAR_TIME = _char("0004")
# (oldest, newest) timestamps of the stored history.
CHAR_FIRST_REC = _char("0005")
CHAR_LED_SENSITIVITY = _char("0008")
# The cloud API key. Reading it doubles as the check that authentication worked.
CHAR_API_KEY = _char("0009")
# The serial number, which is also the cloud device ID.
CHAR_SERIAL = _char("0010")
# Must be written with the pairing code before anything else in the service answers.
CHAR_PAIRING_CODE = _char("0011")
# Minutes between live measurement notifications. Only byte 0 is significant.
CHAR_READING_BATCH_SIZE = _char("0013")

# Standard Battery Service / Battery Level.
CHAR_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# Stored records sit on an exact one-minute grid, whatever the reading batch size.
RECORD_INTERVAL_S = 60

PAIRING_CODE_MAX = 999_999
READING_BATCH_SIZE_MIN = 1
READING_BATCH_SIZE_MAX = 60


class PowerpalProtocolError(ValueError):
    """A payload from the Powerpal didn't have the expected shape."""


@dataclass(frozen=True, slots=True)
class Measurement:
    """One stored record: the meter pulses counted in the minute starting at `timestamp`."""

    timestamp: int
    pulses: int


@dataclass(frozen=True, slots=True)
class HistoryRange:
    """Timestamps of the oldest and newest records the Powerpal holds."""

    oldest: int
    newest: int


def encode_pairing_code(code: int) -> bytes:
    """Encode the 6-digit pairing code printed on the Powerpal's info card."""
    if not 0 <= code <= PAIRING_CODE_MAX:
        raise ValueError(f"pairing code must be 0-{PAIRING_CODE_MAX}, got {code}")
    return struct.pack("<I", code)


def encode_uint32(value: int) -> bytes:
    """Encode a reading batch size or a timestamp."""
    return struct.pack("<I", value)


def decode_uint32(data: bytes | bytearray) -> int:
    """Decode a reading batch size or a timestamp."""
    if len(data) < 4:
        raise PowerpalProtocolError(f"expected 4 bytes, got {len(data)}: {bytes(data).hex()}")
    return struct.unpack_from("<I", data)[0]


def encode_history_request(start: int, end: int) -> bytes:
    """Encode a CHAR_MEASUREMENT_ACCESS request. Both ends are inclusive.

    A start older than anything stored is clamped by the Powerpal, not rejected.
    """
    if start > end:
        raise ValueError(f"history start {start} is after end {end}")
    return struct.pack("<II", start, end)


def decode_history_range(data: bytes | bytearray) -> HistoryRange:
    """Decode CHAR_FIRST_REC."""
    if len(data) < 8:
        raise PowerpalProtocolError(f"expected 8 byte history range, got {len(data)}")
    oldest, newest = struct.unpack_from("<II", data)
    return HistoryRange(oldest=oldest, newest=newest)


def decode_measurement(data: bytes | bytearray) -> Measurement:
    """Decode a measurement notification.

    Packets are 20 bytes, one record each. Bytes 6-19 are identical in every packet
    observed so far, static device state rather than part of the record.
    """
    if len(data) < 6:
        raise PowerpalProtocolError(f"measurement too short ({len(data)} bytes): {bytes(data).hex()}")
    timestamp, pulses = struct.unpack_from("<IH", data)
    return Measurement(timestamp=timestamp, pulses=pulses)


def decode_single_byte(data: bytes | bytearray) -> int:
    """Decode the battery level or LED sensitivity."""
    if len(data) != 1:
        raise PowerpalProtocolError(f"expected 1 byte, got {len(data)}: {bytes(data).hex()}")
    return data[0]


def decode_device_id(data: bytes | bytearray) -> str:
    """Decode the serial number, e.g. a7 2b 00 00 -> "00002ba7"."""
    if not data:
        raise PowerpalProtocolError("empty serial number")
    return bytes(reversed(data)).hex()


def decode_api_key(data: bytes | bytearray) -> str:
    """Decode the cloud API key, which is stored as raw UUID bytes in big endian order."""
    if len(data) != 16:
        raise PowerpalProtocolError(f"expected 16 byte API key, got {len(data)}")
    return str(uuid.UUID(bytes=bytes(data)))
