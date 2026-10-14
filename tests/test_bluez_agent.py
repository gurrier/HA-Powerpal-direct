"""The BlueZ pairing agent: what it approves, and its registration lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from dbus_fast import DBusError, MessageType
from dbus_fast.service import ServiceInterface
import pytest

from custom_components.powerpal_direct import bluez_agent

from .fakes import ADDRESS, PAIRING_CODE

OUR_PATH = "/org/bluez/hci0/" + bluez_agent.device_path_suffix(ADDRESS)
OTHER_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def handler(name: str):
    """The function BlueZ's calls reach, as dbus-fast dispatches them."""
    agent = bluez_agent._make_agent(ADDRESS, PAIRING_CODE)
    method = next(m for m in ServiceInterface._get_methods(agent) if m.name == name)
    return lambda *args: method.fn(agent, *args)


def test_signatures_match_bluez_agent1() -> None:
    agent = bluez_agent._make_agent(ADDRESS, PAIRING_CODE)
    methods = {m.name: (m.in_signature, m.out_signature) for m in ServiceInterface._get_methods(agent)}
    assert methods == {
        "Release": ("", ""),
        "RequestPinCode": ("o", "s"),
        "DisplayPinCode": ("os", ""),
        "RequestPasskey": ("o", "u"),
        "DisplayPasskey": ("ouq", ""),
        "RequestConfirmation": ("ou", ""),
        "RequestAuthorization": ("o", ""),
        "AuthorizeService": ("os", ""),
        "Cancel": ("", ""),
    }


def test_approves_the_powerpal() -> None:
    assert handler("RequestPasskey")(OUR_PATH) == PAIRING_CODE
    assert handler("RequestPinCode")(OUR_PATH) == "123123"
    handler("RequestConfirmation")(OUR_PATH, 0)
    handler("RequestAuthorization")(OUR_PATH)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("RequestPasskey", (OTHER_PATH,)),
        ("RequestConfirmation", (OTHER_PATH, 0)),
        ("RequestAuthorization", (OTHER_PATH,)),
        ("AuthorizeService", (OTHER_PATH, "0000180f-0000-1000-8000-00805f9b34fb")),
    ],
)
def test_rejects_any_other_device(name: str, args: tuple) -> None:
    with pytest.raises(DBusError):
        handler(name)(*args)


def ok_reply() -> MagicMock:
    return MagicMock(message_type=MessageType.METHOD_RETURN)


async def test_registers_as_default_then_unregisters() -> None:
    bus = MagicMock()
    bus.call = AsyncMock(return_value=ok_reply())
    with patch.object(bluez_agent, "_connect_system_bus", AsyncMock(return_value=bus)):
        async with bluez_agent.pairing_agent(ADDRESS, PAIRING_CODE) as registered:
            assert registered
            members = [c.args[0].member for c in bus.call.await_args_list]
            assert members == ["RegisterAgent", "RequestDefaultAgent"]
            assert bus.call.await_args_list[0].args[0].body == [bluez_agent.AGENT_PATH, "KeyboardDisplay"]
    assert bus.call.await_args_list[-1].args[0].member == "UnregisterAgent"
    bus.disconnect.assert_called_once()


async def test_registration_refused_still_lets_the_connection_go_ahead() -> None:
    bus = MagicMock()
    bus.call = AsyncMock(
        return_value=MagicMock(message_type=MessageType.ERROR, error_name="org.bluez.Error.AlreadyExists", body=[])
    )
    with patch.object(bluez_agent, "_connect_system_bus", AsyncMock(return_value=bus)):
        async with bluez_agent.pairing_agent(ADDRESS, PAIRING_CODE) as registered:
            assert not registered
    assert [c.args[0].member for c in bus.call.await_args_list] == ["RegisterAgent"]
    bus.disconnect.assert_called_once()


async def test_no_system_bus_is_a_no_op() -> None:
    with patch.object(bluez_agent, "_connect_system_bus", AsyncMock(return_value=None)):
        async with bluez_agent.pairing_agent(ADDRESS, PAIRING_CODE) as registered:
            assert not registered
