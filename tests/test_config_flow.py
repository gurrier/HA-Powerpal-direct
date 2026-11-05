# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Config flow: discovery, pairing code check, reauth and options."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.powerpal_direct.client import DeviceIdentity, PowerpalAuthError, PowerpalBondError
from custom_components.powerpal_direct.config_flow import CONF_TAKE_OVER
from custom_components.powerpal_direct.const import (
    CONF_COST_PER_KWH,
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)
from custom_components.powerpal_direct.gateway import GatewayRates

from .fakes import ADDRESS, API_KEY, DEVICE_ID, service_info

FLOW = "custom_components.powerpal_direct.config_flow"


@pytest.fixture(autouse=True)
def bluetooth_loaded(hass: HomeAssistant) -> Generator[None]:
    """Stand in for the Bluetooth stack, which the tests don't run."""
    hass.config.components.add("bluetooth_adapters")
    with (
        patch(f"{FLOW}.bluetooth.async_last_service_info", return_value=None),
        patch(f"{FLOW}.bluetooth.async_discovered_service_info", return_value=[service_info()]),
        patch("custom_components.powerpal_direct.async_setup_entry", return_value=True),
    ):
        yield


@pytest.fixture
def validate() -> Generator[AsyncMock]:
    with patch(
        f"{FLOW}._async_validate", return_value=DeviceIdentity(device_id=DEVICE_ID, api_key=API_KEY)
    ) as validate:
        yield validate


async def test_bluetooth_discovery(hass: HomeAssistant, validate: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: " 123123 ", CONF_PULSES_PER_KWH: 800}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Powerpal 00002ba7"
    assert result["result"].unique_id == ADDRESS
    assert result["data"] == {CONF_PAIRING_CODE: 123123}
    assert result["options"] == {
        CONF_PULSES_PER_KWH: 800.0,
        CONF_NOTIFICATION_INTERVAL: 1,
        CONF_POLL_INTERVAL: 0,
        CONF_COST_PER_KWH: 0.0,
    }
    assert validate.call_args.args[1] == 123123


async def test_user_picks_discovered_device(hass: HomeAssistant, validate: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 1000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_with_nothing_advertising(hass: HomeAssistant) -> None:
    with patch(f"{FLOW}.bluetooth.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


@pytest.mark.parametrize(
    ("code", "side_effect", "error"),
    [
        ("12312", None, {CONF_PAIRING_CODE: "invalid_pairing_code"}),
        ("12a123", None, {CONF_PAIRING_CODE: "invalid_pairing_code"}),
        ("123123", PowerpalAuthError("nope"), {"base": "invalid_auth"}),
        ("123123", PowerpalBondError("AuthenticationFailed"), {"base": "cannot_bond"}),
        ("123123", TimeoutError(), {"base": "cannot_connect"}),
    ],
)
async def test_pair_errors_then_recover(
    hass: HomeAssistant, validate: AsyncMock, code: str, side_effect: Exception | None, error: dict[str, str]
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    good = validate.return_value
    validate.side_effect = side_effect
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: code, CONF_PULSES_PER_KWH: 1000}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == error

    validate.side_effect = None
    validate.return_value = good
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 1000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_with_the_only_powerpal_already_configured(hass: HomeAssistant) -> None:
    """A configured Powerpal is connected, so it doesn't advertise: say it's set up, not missing."""
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Powerpal 00002ba7").add_to_hass(hass)
    with patch(f"{FLOW}.bluetooth.async_discovered_service_info", return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "all_configured"
    assert result["description_placeholders"] == {"names": "Powerpal 00002ba7"}


async def test_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, validate: AsyncMock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_PAIRING_CODE: 111111})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PAIRING_CODE: "123123"})
    assert result["errors"] == {"base": "not_advertising"}

    with patch(f"{FLOW}.bluetooth.async_last_service_info", return_value=service_info()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PAIRING_CODE: "123123"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PAIRING_CODE] == 123123


async def test_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        data={CONF_PAIRING_CODE: 123123},
        options={CONF_PULSES_PER_KWH: 1000.0, CONF_NOTIFICATION_INTERVAL: 1, CONF_POLL_INTERVAL: 0},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PULSES_PER_KWH: 800, CONF_NOTIFICATION_INTERVAL: 1, CONF_POLL_INTERVAL: 15, CONF_COST_PER_KWH: 0.25},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_PULSES_PER_KWH: 800.0,
        CONF_NOTIFICATION_INTERVAL: 1,
        CONF_POLL_INTERVAL: 15,
        CONF_COST_PER_KWH: 0.25,
    }


async def test_settings_found_from_an_esp32_gateway(hass: HomeAssistant, validate: AsyncMock) -> None:
    with patch(
        f"{FLOW}.async_detect_gateway_rates", return_value=GatewayRates(pulses_per_kwh=800.0, cost_per_kwh=0.25)
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
        )
    assert "800" in result["description_placeholders"]["gateway_note"]
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if callable(getattr(key, "default", None))
    }
    assert defaults[CONF_PULSES_PER_KWH] == 800.0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 800}
    )
    assert result["options"][CONF_PULSES_PER_KWH] == 800.0
    assert result["options"][CONF_COST_PER_KWH] == 0.25


async def test_no_gateway_no_note(hass: HomeAssistant, validate: AsyncMock) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["description_placeholders"]["gateway_note"] == ""


async def test_reconfigure_enters_a_new_code(hass: HomeAssistant, validate: AsyncMock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_PAIRING_CODE: 111111})
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch(f"{FLOW}.bluetooth.async_last_service_info", return_value=service_info()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PAIRING_CODE: "123123"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PAIRING_CODE] == 123123


def _register_live_gateway(hass: HomeAssistant, esphome_entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        "esphome",
        "84:CC:A8:52:12:7C/0/switch/Powerpal BLE Connection",
        suggested_object_id="bedroom_powerpal_gateway_powerpal_ble_connection",
        config_entry=esphome_entry,
    )
    registry.async_get_or_create(
        "sensor",
        "esphome",
        "84:CC:A8:52:12:7C/0/sensor/Powerpal Total Energy",
        suggested_object_id="powerpal_gateway_powerpal_total_energy",
        config_entry=esphome_entry,
    )
    hass.states.async_set("switch.bedroom_powerpal_gateway_powerpal_ble_connection", "on")
    hass.states.async_set("sensor.powerpal_gateway_powerpal_total_energy", "1821.9")


@pytest.fixture
def gateway_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="esphome", unique_id="84:cc:a8:52:12:7c", title="BLEProxy 52127c")
    entry.add_to_hass(hass)
    _register_live_gateway(hass, entry)
    return entry


async def test_takes_over_from_a_running_gateway(
    hass: HomeAssistant, validate: AsyncMock, gateway_entry: MockConfigEntry
) -> None:
    turn_off = async_mock_service(hass, "switch", "turn_off")
    discovered: list = []
    with (
        patch(f"{FLOW}.bluetooth.async_discovered_service_info", side_effect=lambda *a, **k: list(discovered)),
        patch(f"{FLOW}.GATEWAY_RELEASE_POLL_S", 0.01),
        patch(f"{FLOW}.GATEWAY_RELEASE_TIMEOUT_S", 1),
    ):
        # Nothing discovered: the gateway holds the Powerpal, so the take-over is offered.
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["step_id"] == "gateway"
        assert result["description_placeholders"]["gateway"] == "BLEProxy 52127c"

        async def appear() -> None:
            await asyncio.sleep(0.05)  # the Powerpal advertises once the gateway lets go
            discovered.append(service_info())

        hass.async_create_task(appear())
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TAKE_OVER: True})
        assert result["step_id"] == "pair"
        assert [c.data["entity_id"] for c in turn_off] == ["switch.bedroom_powerpal_gateway_powerpal_ble_connection"]
        assert "BLEProxy 52127c" in result["description_placeholders"]["gateway_note"]
        # The gateway is untouched until pairing succeeds.
        assert hass.config_entries.async_get_entry(gateway_entry.entry_id).disabled_by is None

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 1000}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ADDRESS
    assert hass.config_entries.async_get_entry(gateway_entry.entry_id).disabled_by is ConfigEntryDisabler.USER


async def test_gateway_that_keeps_the_powerpal(hass: HomeAssistant, gateway_entry: MockConfigEntry) -> None:
    async_mock_service(hass, "switch", "turn_off")
    with (
        patch(f"{FLOW}.bluetooth.async_discovered_service_info", return_value=[]),
        patch(f"{FLOW}.GATEWAY_RELEASE_POLL_S", 0.01),
        patch(f"{FLOW}.GATEWAY_RELEASE_TIMEOUT_S", 0.05),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TAKE_OVER: True})
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "gateway_still_connected"}
    assert hass.config_entries.async_get_entry(gateway_entry.entry_id).disabled_by is None


async def test_declining_the_take_over(
    hass: HomeAssistant, validate: AsyncMock, gateway_entry: MockConfigEntry
) -> None:
    turn_off = async_mock_service(hass, "switch", "turn_off")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "gateway"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TAKE_OVER: False})
    assert result["step_id"] == "user"  # the fixture's discovered Powerpal, set up alongside
    assert turn_off == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 1000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.async_get_entry(gateway_entry.entry_id).disabled_by is None


async def test_discovered_while_the_gateway_entry_is_still_enabled(
    hass: HomeAssistant, validate: AsyncMock, gateway_entry: MockConfigEntry
) -> None:
    # The ESP32 is off (the Powerpal advertises) but its entry was never disabled: pairing
    # succeeds, and the entry is disabled so the records can be adopted at setup.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["step_id"] == "pair"
    assert "BLEProxy 52127c" in result["description_placeholders"]["gateway_note"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PAIRING_CODE: "123123", CONF_PULSES_PER_KWH: 1000}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.async_get_entry(gateway_entry.entry_id).disabled_by is ConfigEntryDisabler.USER
