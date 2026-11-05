# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""A BlueZ pairing agent, registered only while connecting to a Powerpal.

The Powerpal only accepts a bond requested with MITM protection, even though the
ceremony itself is Just Works. The kernel asks for MITM only when the pairing agent's IO
capability allows some input or output, and with no agent registered BlueZ pairs as
NoInputNoOutput. The Powerpal then fails the pairing with AuthenticationFailed, which is
what a local adapter in Home Assistant OS does without this. Phones, Windows and the
ESP32 all ask for MITM, which is why they bond.

The agent only answers for the one Powerpal being connected to, and gives a passkey
request the pairing code, in case a Powerpal asks for one. On anything but Linux, or
without a system bus, it does nothing, and an ESPHome proxy pairs without it.

No `from __future__ import annotations` here: dbus-fast reads the D-Bus signatures from
the annotations, and they must stay plain strings.
"""

import asyncio
from collections.abc import AsyncIterator
import contextlib
import logging
import sys

_LOGGER = logging.getLogger(__name__)

AGENT_PATH = "/org/homeassistant/powerpal_direct/agent"
# Anything but NoInputNoOutput makes the kernel request MITM. Against the Powerpal's own
# capability the method still comes out as Just Works, so nothing needs entering.
CAPABILITY = "KeyboardDisplay"
BLUEZ_SERVICE = "org.bluez"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
REJECTED = "org.bluez.Error.Rejected"

# There's one agent path and one default agent, so only one connection uses it at a time.
_LOCK = asyncio.Lock()


def device_path_suffix(address: str) -> str:
    """The end of BlueZ's object path for a device, e.g. /org/bluez/hci0/dev_D5_07_..."""
    return "dev_" + address.upper().replace(":", "_")


def _make_agent(address: str, passkey: int):
    from dbus_fast import DBusError
    from dbus_fast.service import ServiceInterface, dbus_method

    suffix = device_path_suffix(address)

    class PowerpalPairingAgent(ServiceInterface):
        """org.bluez.Agent1, approving pairing with one Powerpal and nothing else."""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @staticmethod
        def _check(device: str) -> None:
            if not device.endswith(suffix):
                _LOGGER.debug("Pairing agent rejecting a request for %s", device)
                raise DBusError(REJECTED, "Only pairs with the Powerpal being set up")

        @dbus_method()
        def Release(self) -> None:
            pass

        @dbus_method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: F821
            self._check(device)
            return f"{passkey:06d}"

        @dbus_method()
        def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821
            self._check(device)

        @dbus_method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: F821
            self._check(device)
            _LOGGER.debug("%s asked for a passkey; answering with the pairing code", address)
            return passkey

        @dbus_method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821
            self._check(device)

        @dbus_method()
        def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821
            self._check(device)
            _LOGGER.debug("Confirming pairing with %s", address)

        @dbus_method()
        def RequestAuthorization(self, device: "o") -> None:  # noqa: F821
            self._check(device)
            _LOGGER.debug("Authorizing pairing with %s", address)

        @dbus_method()
        def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821
            self._check(device)

        @dbus_method()
        def Cancel(self) -> None:
            _LOGGER.debug("BlueZ cancelled pairing with %s", address)

    return PowerpalPairingAgent()


async def _call(bus, member: str, signature: str, body: list) -> None:
    from dbus_fast import Message, MessageType

    reply = await bus.call(
        Message(
            destination=BLUEZ_SERVICE,
            path=AGENT_MANAGER_PATH,
            interface=AGENT_MANAGER_INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )
    )
    if reply is None or reply.message_type == MessageType.ERROR:
        error = reply.error_name if reply is not None else "no reply"
        raise RuntimeError(f"{member} failed: {error} {reply.body if reply is not None else ''}")


@contextlib.asynccontextmanager
async def pairing_agent(address: str, passkey: int) -> AsyncIterator[bool]:
    """Register the agent as BlueZ's default for the duration. Yields whether it is.

    It has to be the default agent, not just a registered one: bleak asks BlueZ to pair
    over its own D-Bus connection, and BlueZ uses the caller's agent or else the default.
    """
    async with _LOCK, _registered(address, passkey) as registered:
        yield registered


@contextlib.asynccontextmanager
async def _registered(address: str, passkey: int) -> AsyncIterator[bool]:
    bus = await _connect_system_bus()
    if bus is None:
        yield False
        return

    registered = False
    try:
        bus.export(AGENT_PATH, _make_agent(address, passkey))
        try:
            await _call(bus, "RegisterAgent", "os", [AGENT_PATH, CAPABILITY])
            registered = True
            await _call(bus, "RequestDefaultAgent", "o", [AGENT_PATH])
            _LOGGER.debug("%s: Pairing agent registered (%s)", address, CAPABILITY)
        except RuntimeError as err:
            _LOGGER.warning("%s: Couldn't register a Bluetooth pairing agent: %s", address, err)
        yield registered
    finally:
        if registered:
            try:
                await _call(bus, "UnregisterAgent", "o", [AGENT_PATH])
            except RuntimeError as err:
                _LOGGER.debug("%s: Unregistering the pairing agent failed: %s", address, err)
        bus.disconnect()


async def _connect_system_bus():
    if not sys.platform.startswith("linux"):
        return None
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        return await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=False).connect()
    except Exception as err:
        _LOGGER.debug("No system D-Bus for a pairing agent: %s", err)
        return None
