"""Taking over from an esphome-powerpal_ble gateway: its entity IDs and its last readings.

Home Assistant keeps state history and long-term statistics against entity IDs. Once the
gateway's ESPHome device has been removed, its entity IDs are free, and Powerpal Direct
can register its own entities under them, so graphs, statistics, the Energy dashboard,
utility meters and automations carry on as if nothing changed.

The gateway's entities are recognised by the names the ESPHome component documents
("Powerpal Total Energy" and so on), which ESPHome puts in each entity's unique ID. Home
Assistant remembers removed entities, so they're still found after the device is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util, slugify

_LOGGER = logging.getLogger(__name__)

ESPHOME = "esphome"

# Powerpal Direct entity key -> the gateway entity's name in esphome-powerpal_ble's
# documented config. Only entities that mean the same thing are listed: the gateway's
# watchdog entities count ESP32 restarts, which Powerpal Direct doesn't have, and the API
# key is never exposed as an entity (it's read from the Powerpal whenever it's needed).
GATEWAY_NAMES: dict[str, str] = {
    "power": "Powerpal Power",
    "energy": "Powerpal Total Energy",
    "daily_energy": "Powerpal Daily Energy",
    "battery": "Powerpal Battery",
    "led_sensitivity": "Powerpal LED Sensitivity",
    "last_reading": "Powerpal Timestamp",
    "last_pulses": "Powerpal Pulses",
    "last_watt_hours": "Powerpal Watt Hours",
    "last_cost": "Powerpal Cost",
    "device_id": "Powerpal Device ID",
    "advertisement_rssi": "Powerpal Advertisement RSSI",
    "advertisement_rate": "Powerpal Advertisement Rate",
    "last_stall": "Powerpal Last Stall",
    "connection": "Powerpal BLE Connection",
}
_KEY_BY_SLUG = {slugify(name): key for key, name in GATEWAY_NAMES.items()}


@dataclass(slots=True)
class GatewayEntities:
    """What was found of a gateway in the entity registry."""

    # Powerpal Direct entity key -> a removed gateway entity's ID, free to take over.
    available: dict[str, str]
    # Gateway entities still registered, whose IDs can't be taken until they're removed.
    still_present: dict[str, str]


def _gateway_key(unique_id: str) -> str | None:
    """Our entity key for an ESPHome unique ID, if it's one of the gateway's.

    ESPHome unique IDs end in the entity's name ("MAC/0/sensor/Powerpal Power") or, in
    older versions, its object ID ("mac-sensor-powerpal_power").
    """
    tail = unique_id.rsplit("/", 1)[-1] if "/" in unique_id else unique_id.rsplit("-", 1)[-1]
    return _KEY_BY_SLUG.get(slugify(tail))


def find_gateway_entities(hass: HomeAssistant) -> GatewayEntities:
    registry = er.async_get(hass)
    still_present: dict[str, str] = {}
    for entry in registry.entities.values():
        if entry.platform == ESPHOME and (key := _gateway_key(entry.unique_id)):
            still_present[key] = entry.entity_id

    available: dict[str, str] = {}
    # Oldest first, so the most recently removed gateway wins if there were several.
    for deleted in sorted(registry.deleted_entities.values(), key=lambda d: d.modified_at):
        if deleted.platform != ESPHOME or (key := _gateway_key(deleted.unique_id)) is None:
            continue
        if key in still_present:
            continue
        if registry.async_get(deleted.entity_id) is not None or hass.states.get(deleted.entity_id) is not None:
            continue
        available[key] = deleted.entity_id
    return GatewayEntities(available=available, still_present=still_present)


@dataclass(slots=True)
class GatewayReadings:
    """The gateway's last recorded values, to carry on from."""

    total_kwh: float | None = None
    daily_kwh: float | None = None
    daily_date: str | None = None
    last_reading: int | None = None


async def async_last_gateway_readings(hass: HomeAssistant, entity_ids: dict[str, str]) -> GatewayReadings:
    """Read the gateway's last Total Energy, Daily Energy and Timestamp from the recorder."""
    readings = GatewayReadings()
    wanted = {key: entity_ids[key] for key in ("energy", "daily_energy", "last_reading") if key in entity_ids}
    if not wanted or "recorder" not in hass.config.components:
        return readings

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_last_state_changes

    def _last(entity_id: str) -> tuple[str, datetime] | None:
        # The newest state that was an actual value, skipping unavailable/unknown.
        states = get_last_state_changes(hass, 20, entity_id).get(entity_id, [])
        for state in reversed(states):
            if state.state not in ("unknown", "unavailable", ""):
                return state.state, state.last_changed
        return None

    recorder = get_instance(hass)
    for key, entity_id in wanted.items():
        try:
            last = await recorder.async_add_executor_job(_last, entity_id)
        except Exception as err:
            _LOGGER.debug("Couldn't read %s's last state: %s", entity_id, err)
            continue
        if last is None:
            continue
        value, changed = last
        try:
            if key == "energy":
                readings.total_kwh = float(value)
            elif key == "daily_energy":
                readings.daily_kwh = float(value)
                readings.daily_date = dt_util.as_local(changed).date().isoformat()
            elif (parsed := dt_util.parse_datetime(value)) is not None:
                readings.last_reading = int(parsed.timestamp())
        except ValueError:
            _LOGGER.debug("Ignoring %s's last state %r", entity_id, value)
    return readings


# Meter constants are printed as round numbers; a detected rate this close to one is it.
COMMON_PULSES_PER_KWH = (
    100,
    200,
    250,
    400,
    500,
    600,
    800,
    1000,
    1200,
    1600,
    2000,
    2400,
    3200,
    3600,
    4000,
    5000,
    6400,
    8000,
    10000,
)
MIN_PULSES_FOR_RATE = 50
RATE_HISTORY_STATES = 60


@dataclass(slots=True)
class GatewayRates:
    """The gateway's settings, worked out from what it recorded."""

    pulses_per_kwh: float | None = None
    cost_per_kwh: float | None = None


def all_gateway_entity_ids(gateway: GatewayEntities) -> dict[str, str]:
    """Removed and still-present gateway entities alike: both have history to read."""
    return {**gateway.available, **gateway.still_present}


def rates_from_readings(readings: list[tuple[int, int, float | None]]) -> GatewayRates:
    """Work out pulses per kWh and cost per kWh from (pulses, watt hours, cost) readings.

    The gateway published Watt Hours = round(Pulses / pulses_per_kwh * 1000). At high rates
    each reading is only a few Wh, and that rounding skews a plain ratio (3200 comes out
    as ~3130), so the standard meter rates are tried first: the one that reproduces the
    recorded Watt Hours exactly is the answer. A plain ratio is the fallback for a meter
    with an unusual rate.
    """
    usable = [(pulses, wh, cost) for pulses, wh, cost in readings if pulses > 0]
    total_pulses = sum(pulses for pulses, _, _ in usable)
    if total_pulses < MIN_PULSES_FOR_RATE or not any(wh for _, wh, _ in usable):
        return GatewayRates()

    def matches(rate: float) -> int:
        return sum(round(pulses / rate * 1000) == wh for pulses, wh, _ in usable)

    ratio = total_pulses * 1000 / sum(wh for _, wh, _ in usable)
    # If a short history fits more than one, the one nearest the plain ratio.
    exact = [rate for rate in COMMON_PULSES_PER_KWH if matches(rate) == len(usable)]
    pulses_per_kwh = float(min(exact, key=lambda rate: abs(rate - ratio)) if exact else round(ratio))

    cost_per_kwh = None
    costs = [(pulses, cost) for pulses, _, cost in usable if cost is not None]
    if costs:
        cost_per_kwh = round(sum(cost for _, cost in costs) / sum(p for p, _ in costs) * pulses_per_kwh, 4)
    return GatewayRates(pulses_per_kwh=pulses_per_kwh, cost_per_kwh=cost_per_kwh)


async def async_detect_gateway_rates(hass: HomeAssistant, gateway: GatewayEntities) -> GatewayRates:
    """The gateway's pulses per kWh and cost per kWh, from its recorded Pulses, Watt Hours and Cost."""
    entity_ids = all_gateway_entity_ids(gateway)
    if "last_pulses" not in entity_ids or "last_watt_hours" not in entity_ids:
        return GatewayRates()
    if "recorder" not in hass.config.components:
        return GatewayRates()

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_last_state_changes

    def _numbers(entity_id: str | None) -> dict[int, float]:
        """Recent numeric states, keyed by the second they were recorded."""
        if entity_id is None:
            return {}
        values: dict[int, float] = {}
        for state in get_last_state_changes(hass, RATE_HISTORY_STATES, entity_id).get(entity_id, []):
            try:
                values[int(state.last_updated.timestamp())] = float(state.state)
            except ValueError:
                continue
        return values

    def _read() -> list[tuple[int, int, float | None]]:
        pulses = _numbers(entity_ids["last_pulses"])
        watt_hours = _numbers(entity_ids["last_watt_hours"])
        costs = _numbers(entity_ids.get("last_cost"))
        readings = []
        # The gateway published all three together; pair them by the second recorded.
        for second, count in pulses.items():
            wh = next((watt_hours[s] for s in (second, second - 1, second + 1) if s in watt_hours), None)
            if wh is None:
                continue
            cost = next((costs[s] for s in (second, second - 1, second + 1) if s in costs), None)
            readings.append((int(count), int(wh), cost))
        return readings

    try:
        readings = await get_instance(hass).async_add_executor_job(_read)
    except Exception as err:
        _LOGGER.debug("Couldn't read the gateway's recent readings: %s", err)
        return GatewayRates()
    return rates_from_readings(readings)
