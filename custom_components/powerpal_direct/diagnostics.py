"""Diagnostics for Powerpal Direct."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_PAIRING_CODE
from .coordinator import PowerpalConfigEntry

TO_REDACT = {CONF_PAIRING_CODE, "api_key"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PowerpalConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = asdict(coordinator.data)
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "data": async_redact_data(data, TO_REDACT),
        "energy": coordinator.energy.state.as_dict(),
        "power_available": coordinator.power_available,
    }
