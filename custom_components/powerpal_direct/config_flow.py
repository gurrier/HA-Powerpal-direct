# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Config flow for Powerpal Direct."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from bleak.exc import BleakError
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

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
from .gateway import GatewayEntities, GatewayRates, async_detect_gateway_rates, find_gateway_entities

_LOGGER = logging.getLogger(__name__)

# After the gateway's BLE connection is switched off, the Powerpal notices within seconds
# and starts advertising; give the scanners a while to hear it.
GATEWAY_RELEASE_TIMEOUT_S = 45
GATEWAY_RELEASE_POLL_S = 2
CONF_TAKE_OVER = "take_over"

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
        # A running ESP32 gateway's ESPHome entry, to disable once pairing succeeds so its
        # entity records can be adopted.
        self._gateway_entry: ConfigEntry | None = None
        self._gateway_declined = False

    def _async_find_live_gateway(self) -> tuple[ConfigEntry, GatewayEntities] | None:
        """The ESP32 gateway's ESPHome entry, if it's enabled and its entities are registered."""
        gateway = find_gateway_entities(self.hass)
        registry = er.async_get(self.hass)
        for entity_id in gateway.still_present.values():
            entry = registry.async_get(entity_id)
            if entry is None or entry.config_entry_id is None:
                continue
            config_entry = self.hass.config_entries.async_get_entry(entry.config_entry_id)
            if config_entry is not None and config_entry.disabled_by is None:
                return config_entry, gateway
        return None

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

        # A running gateway holds the Powerpal's only connection, so it can't even be
        # discovered until the gateway lets go. Offer to do that here.
        if not self._gateway_declined and self._async_find_live_gateway() is not None:
            return await self.async_step_gateway()

        self._async_collect_discovered()
        if not self._discovered:
            # A configured Powerpal is connected to Home Assistant, so it doesn't advertise
            # and isn't listed. Someone who clicks Add after installing through HACS, with
            # the integration already running, lands here: say so, rather than blaming a
            # gateway or the app.
            if configured := [entry.title for entry in self._async_current_entries(include_ignore=False)]:
                return self.async_abort(
                    reason="all_configured", description_placeholders={"names": ", ".join(configured)}
                )
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

    @callback
    def _async_collect_discovered(self) -> None:
        configured = self._async_current_ids(include_ignore=False)
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if info.address not in configured and is_powerpal(info):
                self._discovered[info.address] = info

    async def async_step_gateway(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take over from a running ESP32 gateway: release the Powerpal, then pair."""
        found = self._async_find_live_gateway()
        if found is None:
            return await self.async_step_user()
        gateway_entry, gateway = found
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_TAKE_OVER, True):
                self._gateway_declined = True
                return await self.async_step_user()
            self._gateway_entry = gateway_entry
            await self._async_release_gateway(gateway)
            if await self._async_wait_for_powerpal():
                address = next(iter(self._discovered))
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._discovery = self._discovered[address]
                return await self.async_step_pair()
            errors["base"] = "gateway_still_connected"

        return self.async_show_form(
            step_id="gateway",
            data_schema=vol.Schema({vol.Required(CONF_TAKE_OVER, default=True): bool}),
            errors=errors,
            description_placeholders={"gateway": gateway_entry.title},
        )

    async def _async_release_gateway(self, gateway: GatewayEntities) -> None:
        """Switch the gateway's own BLE connection off, so the Powerpal is free to pair."""
        switch = gateway.still_present.get("connection")
        state = self.hass.states.get(switch) if switch else None
        if switch is None or state is None or state.state != "on":
            return
        _LOGGER.info("Switching off the ESP32 gateway's connection (%s) to release the Powerpal", switch)
        await self.hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)

    async def _async_wait_for_powerpal(self) -> bool:
        for _ in range(int(GATEWAY_RELEASE_TIMEOUT_S / GATEWAY_RELEASE_POLL_S)):
            self._async_collect_discovered()
            if self._discovered:
                return True
            await asyncio.sleep(GATEWAY_RELEASE_POLL_S)
        return False

    async def _async_disable_gateway_entry(self) -> None:
        """Disable the gateway's ESPHome entry, so its entity records can be adopted at setup.

        Only once pairing has succeeded: until then the gateway is still the working setup.
        """
        if self._gateway_entry is None:
            return
        entry = self.hass.config_entries.async_get_entry(self._gateway_entry.entry_id)
        if entry is None or entry.disabled_by is not None:
            return
        _LOGGER.info("Disabling the ESP32 gateway's entry %s; Powerpal Direct takes over its entities", entry.title)
        await self.hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the pairing code and the meter's pulse rate, and check the code works."""
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if (
            not self._gateway_declined
            and self._gateway_entry is None
            and (found := self._async_find_live_gateway()) is not None
        ):
            # Discovered while the gateway's entry is still enabled (the ESP32 is off).
            self._gateway_entry = found[0]
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
                await self._async_disable_gateway_entry()
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
                )
                + (
                    f"\n\nThe gateway's entry ({self._gateway_entry.title}) will be disabled once pairing "
                    "succeeds, so its entities carry on under Powerpal Direct."
                    if self._gateway_entry is not None
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
        return await self._async_step_new_code(self._get_reauth_entry(), "reauth_confirm", user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter a new pairing code, e.g. after the no-readings repair issue."""
        return await self._async_step_new_code(self._get_reconfigure_entry(), "reconfigure", user_input)

    async def _async_step_new_code(
        self, entry: PowerpalConfigEntry, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
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
            step_id=step_id,
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
