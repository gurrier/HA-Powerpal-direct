# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Bonding and authentication, against a mocked bleak client."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakDBusError, BleakError
import pytest

from custom_components.powerpal_direct import protocol
from custom_components.powerpal_direct.client import (
    PowerpalAuthError,
    PowerpalBondError,
    PowerpalClient,
    is_bond_failure,
)

from .fakes import API_KEY, BLE_DEVICE, PAIRING_CODE

API_KEY_BYTES = bytes.fromhex(API_KEY.replace("-", ""))
INSUFFICIENT_AUTH = BleakError("Failed to read characteristic 39: Protocol Error 0x05: Insufficient Authentication")


@pytest.fixture(autouse=True)
def no_settle() -> Generator[None]:
    with patch("custom_components.powerpal_direct.client.AUTH_SETTLE_S", 0):
        yield


@pytest.fixture
def bleak_client() -> Generator[MagicMock]:
    client = MagicMock()
    client.is_connected = True
    client.write_gatt_char = AsyncMock()
    client.read_gatt_char = AsyncMock(return_value=bytearray(API_KEY_BYTES))
    client.pair = AsyncMock()
    with patch(
        "custom_components.powerpal_direct.client.establish_connection", AsyncMock(return_value=client)
    ) as connect:
        client.establish_connection = connect
        yield client


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (BleakDBusError("org.bluez.Error.AuthenticationFailed", []), True),
        (BleakError("Pairing failed due to error: 5"), True),
        (INSUFFICIENT_AUTH, True),
        (BleakError("Failed to read characteristic: Protocol Error 0x0e: Unlikely Error"), False),
        (TimeoutError(), False),
    ],
)
def test_is_bond_failure(err: BaseException, expected: bool) -> None:
    assert is_bond_failure(err) is expected


def test_is_bond_failure_follows_the_cause() -> None:
    try:
        try:
            raise BleakDBusError("org.bluez.Error.AuthenticationFailed", [])
        except BleakError as inner:
            raise BleakError("Failed to connect after 3 attempts") from inner
    except BleakError as outer:
        assert is_bond_failure(outer)


async def test_connects_with_pairing_and_writes_code_without_response(bleak_client: MagicMock) -> None:
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    assert bleak_client.establish_connection.call_args.kwargs["pair"] is True

    assert await client.authenticate() == API_KEY
    bleak_client.write_gatt_char.assert_awaited_once_with(
        protocol.CHAR_PAIRING_CODE, bytes.fromhex("f3e00100"), response=True
    )
    bleak_client.pair.assert_not_awaited()


async def test_connect_bond_failure(bleak_client: MagicMock) -> None:
    bleak_client.establish_connection.side_effect = BleakDBusError("org.bluez.Error.AuthenticationFailed", [])
    with pytest.raises(PowerpalBondError):
        await PowerpalClient(BLE_DEVICE, PAIRING_CODE).connect()


async def test_connect_proxy_without_pairing_support(bleak_client: MagicMock) -> None:
    bleak_client.establish_connection.side_effect = NotImplementedError("Pairing is not available")
    with pytest.raises(PowerpalBondError):
        await PowerpalClient(BLE_DEVICE, PAIRING_CODE).connect()


async def test_bonds_explicitly_if_connect_did_not(bleak_client: MagicMock) -> None:
    bleak_client.read_gatt_char.side_effect = [INSUFFICIENT_AUTH, bytearray(API_KEY_BYTES)]
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    assert await client.authenticate() == API_KEY
    bleak_client.pair.assert_awaited_once()


async def test_explicit_bond_fails(bleak_client: MagicMock) -> None:
    bleak_client.read_gatt_char.side_effect = INSUFFICIENT_AUTH
    bleak_client.pair.side_effect = BleakDBusError("org.bluez.Error.AuthenticationFailed", [])
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    with pytest.raises(PowerpalBondError):
        await client.authenticate()


async def test_still_unbonded_after_pairing(bleak_client: MagicMock) -> None:
    bleak_client.read_gatt_char.side_effect = INSUFFICIENT_AUTH
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    with pytest.raises(PowerpalBondError):
        await client.authenticate()


async def test_wrong_code_on_a_bonded_link(bleak_client: MagicMock) -> None:
    rejected = BleakError("Failed to read characteristic 39: Protocol Error 0x0e: Unlikely Error")
    bleak_client.read_gatt_char.side_effect = rejected
    client = PowerpalClient(BLE_DEVICE, 111111)
    await client.connect()
    with pytest.raises(PowerpalAuthError):
        await client.authenticate()


async def test_falls_back_to_write_command_if_the_request_fails(bleak_client: MagicMock) -> None:
    bleak_client.write_gatt_char.side_effect = [TimeoutError(), None]
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    assert await client.authenticate() == API_KEY
    assert [c.kwargs["response"] for c in bleak_client.write_gatt_char.await_args_list] == [True, False]


async def test_unbonded_write_request_leads_to_bonding(bleak_client: MagicMock) -> None:
    bleak_client.write_gatt_char.side_effect = [INSUFFICIENT_AUTH, None]
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    assert await client.authenticate() == API_KEY
    bleak_client.pair.assert_awaited_once()
    assert [c.kwargs["response"] for c in bleak_client.write_gatt_char.await_args_list] == [True, True]


async def test_link_dropping_during_auth_is_transient_not_a_bad_code(bleak_client: MagicMock) -> None:
    # The first attempt fails, the explicit bond goes through, and then the link drops
    # before the second attempt can read anything back.
    attempts = 0

    async def _read(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BleakError("Protocol Error 0x0e: Unlikely Error")
        bleak_client.is_connected = False
        raise BleakError("Not connected")

    bleak_client.read_gatt_char.side_effect = _read
    client = PowerpalClient(BLE_DEVICE, PAIRING_CODE)
    await client.connect()
    with pytest.raises(BleakError) as raised:
        await client.authenticate()
    assert not isinstance(raised.value, PowerpalAuthError)
