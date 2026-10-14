"""Config flow: discovery, pairing code check, reauth and options."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.powerpal_direct.client import DeviceIdentity, PowerpalAuthError, PowerpalBondError
from custom_components.powerpal_direct.const import (
    CONF_COST_PER_KWH,
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DOMAIN,
)

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
