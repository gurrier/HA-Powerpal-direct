"""Config flow for Powerpal Direct."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from bleak.exc import BleakError
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import protocol
from .client import DeviceIdentity, PowerpalAuthError, PowerpalBondError, PowerpalClient
from .const import (
    CONF_COST_PER_KWH,
    CONF_NOTIFICATION_INTERVAL,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    CONF_PULSES_PER_KWH,
    DEFAULT_COST_PER_KWH,
    DEFAULT_NOTIFICATION_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PULSES_PER_KWH,
    DOMAIN,
    POLL_INTERVAL_MAX,
)
from .coordinator import PowerpalConfigEntry
from .gateway import GatewayRates, async_detect_gateway_rates, find_gateway_entities

_LOGGER = logging.getLogger(__name__)

PAIRING_CODE_SELECTOR = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
PULSES_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=100_000, step=1, mode=selector.NumberSelectorMode.BOX)
)


def is_powerpal(info: BluetoothServiceInfoBleak) -> bool:
    return protocol.ADVERTISED_SERVICE_UUID in info.service_uuids or (info.name or "").startswith(
        protocol.LOCAL_NAME_PREFIX
    )


def _parse_pairing_code(value: str) -> int | None:
    value = value.strip()
    if len(value) != 6 or not value.isdigit():
        return None
    return int(value)


async def _async_validate(service_info: BluetoothServiceInfoBleak, pairing_code: int) -> DeviceIdentity:
    """Connect, bond, authenticate and read the device ID, then let go of the Powerpal."""
    client = PowerpalClient(service_info.device, pairing_code)
    try:
        await client.connect()
        api_key = await client.authenticate()
        return await client.read_identity(api_key)
    finally:
        await client.disconnect()


class PowerpalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Powerpal found over Bluetooth."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._gateway_rates: GatewayRates | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: PowerpalConfigEntry) -> PowerpalOptionsFlow:
        return PowerpalOptionsFlow()

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name or discovery_info.address}
        return await self.async_step_pair()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._discovery = self._discovered[address]
            return await self.async_step_pair()

        configured = self._async_current_ids(include_ignore=False)
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if info.address not in configured and is_powerpal(info):
                self._discovered[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{info.name} ({address})" if info.name else address
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the pairing code and the meter's pulse rate, and check the code works."""
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if self._gateway_rates is None:
            # An ESP32 gateway's recorded readings say what its pulses per kWh and cost per
            # kWh were. Pulses per kWh has to match, or the total it carries on from is
            # rescaled, so it's offered as the default rather than left to be retyped.
            self._gateway_rates = await async_detect_gateway_rates(self.hass, find_gateway_entities(self.hass))
        rates = self._gateway_rates
        if user_input is not None:
            code = _parse_pairing_code(user_input[CONF_PAIRING_CODE])
            if code is None:
                errors[CONF_PAIRING_CODE] = "invalid_pairing_code"
            elif (identity := await self._async_try(code, errors)) is not None:
                return self.async_create_entry(
                    title=f"Powerpal {identity.device_id}",
                    data={CONF_PAIRING_CODE: code},
                    options={
                        CONF_PULSES_PER_KWH: float(user_input[CONF_PULSES_PER_KWH]),
                        CONF_NOTIFICATION_INTERVAL: DEFAULT_NOTIFICATION_INTERVAL,
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                        CONF_COST_PER_KWH: (
                            rates.cost_per_kwh if rates.cost_per_kwh is not None else DEFAULT_COST_PER_KWH
                        ),
                    },
                )

        pulses_default = rates.pulses_per_kwh or DEFAULT_PULSES_PER_KWH
        return self.async_show_form(
            step_id="pair",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_PAIRING_CODE): PAIRING_CODE_SELECTOR,
                        vol.Required(CONF_PULSES_PER_KWH, default=pulses_default): PULSES_SELECTOR,
                    }
                ),
                user_input,
            ),
            errors=errors,
            description_placeholders={
                "name": self._discovery.name or self._discovery.address,
                "gateway_note": (
                    f"\n\nPulses per kWh is set to {pulses_default:g}, found from your ESP32 gateway's readings."
                    if rates.pulses_per_kwh
                    else ""
                ),
            },
        )

    async def _async_try(self, code: int, errors: dict[str, str]) -> DeviceIdentity | None:
        assert self._discovery is not None
        # Use the freshest advertisement: the one from discovery may be minutes old.
        latest = bluetooth.async_last_service_info(self.hass, self._discovery.address, connectable=True)
        try:
            return await _async_validate(latest or self._discovery, code)
        except PowerpalBondError as err:
            _LOGGER.debug("Couldn't bond: %s", err)
            errors["base"] = "cannot_bond"
        except PowerpalAuthError as err:
            _LOGGER.debug("Pairing code rejected: %s", err)
            errors["base"] = "invalid_auth"
        except (BleakError, TimeoutError, protocol.PowerpalProtocolError) as err:
            _LOGGER.debug("Couldn't connect: %s", err)
            errors["base"] = "cannot_connect"
        return None

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            code = _parse_pairing_code(user_input[CONF_PAIRING_CODE])
            info = bluetooth.async_last_service_info(self.hass, entry.unique_id or "", connectable=True)
            if code is None:
                errors[CONF_PAIRING_CODE] = "invalid_pairing_code"
            elif info is None:
                # Most likely something else is connected to it, which stops it advertising.
                errors["base"] = "not_advertising"
            else:
                self._discovery = info
                if await self._async_try(code, errors) is not None:
                    return self.async_update_reload_and_abort(entry, data_updates={CONF_PAIRING_CODE: code})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PAIRING_CODE): PAIRING_CODE_SELECTOR}),
            errors=errors,
            description_placeholders={"name": entry.title},
        )


class PowerpalOptionsFlow(OptionsFlow):
    """Meter pulse rate and how to stay in touch with the Powerpal."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PULSES_PER_KWH: float(user_input[CONF_PULSES_PER_KWH]),
                    CONF_NOTIFICATION_INTERVAL: int(user_input[CONF_NOTIFICATION_INTERVAL]),
                    CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                    CONF_COST_PER_KWH: float(user_input[CONF_COST_PER_KWH]),
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PULSES_PER_KWH, default=options.get(CONF_PULSES_PER_KWH, DEFAULT_PULSES_PER_KWH)
                    ): PULSES_SELECTOR,
                    vol.Required(
                        CONF_POLL_INTERVAL, default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=POLL_INTERVAL_MAX,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_NOTIFICATION_INTERVAL,
                        default=options.get(CONF_NOTIFICATION_INTERVAL, DEFAULT_NOTIFICATION_INTERVAL),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=protocol.READING_BATCH_SIZE_MIN,
                            max=protocol.READING_BATCH_SIZE_MAX,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_COST_PER_KWH, default=options.get(CONF_COST_PER_KWH, DEFAULT_COST_PER_KWH)
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=0, max=100, step="any", mode=selector.NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
