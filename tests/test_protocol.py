# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Wire format, checked against the examples in the protocol write-ups."""

from __future__ import annotations

import pytest

from custom_components.powerpal_direct import protocol


def test_pairing_code_is_little_endian_uint32() -> None:
    assert protocol.encode_pairing_code(123123) == bytes.fromhex("f3e00100")


@pytest.mark.parametrize("code", [-1, 1_000_000])
def test_pairing_code_out_of_range(code: int) -> None:
    with pytest.raises(ValueError):
        protocol.encode_pairing_code(code)


def test_measurement_uses_first_six_of_twenty_bytes() -> None:
    packet = bytes.fromhex("888bae6a 0800 0020dc57002000 90d003ff00ffff".replace(" ", ""))
    assert len(packet) == 20
    measurement = protocol.decode_measurement(packet)
    assert measurement.timestamp == 0x6AAE8B88
    assert measurement.pulses == 8


def test_measurement_too_short() -> None:
    with pytest.raises(protocol.PowerpalProtocolError):
        protocol.decode_measurement(b"\x00" * 5)


def test_history_range_and_request_share_a_layout() -> None:
    raw = bytes.fromhex("a4e8ad6a0c8fae6a")
    history = protocol.decode_history_range(raw)
    assert (history.oldest, history.newest) == (0x6AADE8A4, 0x6AAE8F0C)
    assert (history.newest - history.oldest) % protocol.RECORD_INTERVAL_S == 0
    assert protocol.encode_history_request(history.oldest, history.newest) == raw


def test_history_request_rejects_reversed_window() -> None:
    with pytest.raises(ValueError):
        protocol.encode_history_request(2, 1)


def test_device_id_is_reversed_hex() -> None:
    assert protocol.decode_device_id(bytes.fromhex("a72b0000")) == "00002ba7"


def test_api_key_is_big_endian_uuid() -> None:
    raw = bytes.fromhex("95210d4e894f42b7b8824b94df7daa34")
    assert protocol.decode_api_key(raw) == "95210d4e-894f-42b7-b882-4b94df7daa34"


def test_api_key_wrong_length() -> None:
    with pytest.raises(protocol.PowerpalProtocolError):
        protocol.decode_api_key(b"\x00" * 15)


def test_single_byte() -> None:
    assert protocol.decode_single_byte(b"\x09") == 9
    with pytest.raises(protocol.PowerpalProtocolError):
        protocol.decode_single_byte(b"\x09\x00")
