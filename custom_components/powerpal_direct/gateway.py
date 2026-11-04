"""Taking over from an esphome-powerpal_ble gateway: its entities and its last readings.

Home Assistant keeps state history and long-term statistics against entity IDs, and keys
some things (notably the Energy dashboard's cost sensor) to the entity's registry record.
Two ways to carry on from the gateway, in order of preference:

- Adopt: while the gateway's ESPHome entry is disabled, its entities are registered but
  not loaded, and Home Assistant lets them be moved to another integration. Powerpal
  Direct takes the records over whole, so the IDs, names, areas, customisations and the
  cost sensor all survive. Nothing to fix afterwards.
- Rename: once the gateway's entry has been deleted, only its entity IDs remain, in Home
  Assistant's record of removed entities. Powerpal Direct registers its own entities under
  them. History carries on, but the records are new, so the Energy dashboard's cost sensor
  gets recreated at the next restart (the README says how to repair that).

The gateway's entities are recognised by the names the ESPHome component documents
("Powerpal Total Energy" and so on), which ESPHome puts in each entity's unique ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN, MANUFACTURER

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
    # Registered to a disabled ESPHome entry, so the records themselves can be adopted.
    adoptable: dict[str, str]
    # Gateway entities still live, which can't be touched until their entry is disabled.
    still_present: dict[str, str]

    @property
    def readable(self) -> dict[str, str]:
        """Every gateway entity with history worth reading, whichever way it's taken over."""
        return {**self.still_present, **self.available, **self.adoptable}


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
    adoptable: dict[str, str] = {}
    for entry in registry.entities.values():
        if entry.platform != ESPHOME or (key := _gateway_key(entry.unique_id)) is None:
            continue
        config_entry = hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
        # Home Assistant only lets an entity move integration while it isn't loaded, which
        # a disabled entry guarantees.
        if (
            config_entry is not None
            and config_entry.disabled_by is not None
            and hass.states.get(entry.entity_id) is None
        ):
            adoptable[key] = entry.entity_id
        else:
            still_present[key] = entry.entity_id

    available: dict[str, str] = {}
    # Oldest first, so the most recently removed gateway wins if there were several.
    for deleted in sorted(registry.deleted_entities.values(), key=lambda d: d.modified_at):
        if deleted.platform != ESPHOME or (key := _gateway_key(deleted.unique_id)) is None:
            continue
        if key in still_present or key in adoptable:
            continue
        if registry.async_get(deleted.entity_id) is not None or hass.states.get(deleted.entity_id) is not None:
            continue
        available[key] = deleted.entity_id
    return GatewayEntities(available=available, adoptable=adoptable, still_present=still_present)


def async_adopt_gateway_entities(
    hass: HomeAssistant, entry: ConfigEntry, address: str, gateway: GatewayEntities
) -> None:
    """Move a disabled gateway's entity records to Powerpal Direct, before its entities exist.

    Each record keeps its ID, name, area, options and (what matters most) its registry ID,
    so when Powerpal Direct's entity with the same unique ID is added it simply picks the
    record up. The device is created first so the records can point at it.
    """
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        manufacturer=MANUFACTURER,
        name=entry.title,
    )
    for key, entity_id in gateway.adoptable.items():
        try:
            registry.async_update_entity_platform(
                entity_id,
                DOMAIN,
                new_config_entry_id=entry.entry_id,
                new_unique_id=f"{address}_{key}",
                new_device_id=device.id,
            )
        except ValueError as err:
            _LOGGER.warning("Couldn't adopt %s from the ESP32 gateway: %s", entity_id, err)
            continue
        adopted = registry.async_get(entity_id)
        # Disabled only because its old entry was: enable it under ours.
        if adopted is not None and adopted.disabled_by == er.RegistryEntryDisabler.CONFIG_ENTRY:
            registry.async_update_entity(entity_id, disabled_by=None)
        _LOGGER.info("Adopted %s from the ESP32 gateway", entity_id)


def async_take_over_entity_ids(hass: HomeAssistant, address: str, domain: str, gateway: GatewayEntities) -> None:
    """Rename our newly created entities to the removed gateway's entity IDs.

    Entities ask for the gateway's IDs when they're created, but Home Assistant prefers
    an ID it remembers for the same entity from an earlier install: if Powerpal Direct
    was set up before (alongside the gateway, say) and deleted, its old IDs come back.
    Renaming afterwards wins either way. The recorder then keeps writing to the
    gateway's history under that ID.
    """
    registry = er.async_get(hass)
    for key, wanted in gateway.available.items():
        entity_id = registry.async_get_entity_id(wanted.split(".", 1)[0], domain, f"{address}_{key}")
        if entity_id is None or entity_id == wanted:
            continue
        if registry.async_get(wanted) is not None or hass.states.get(wanted) is not None:
            continue
        _LOGGER.info("Taking over %s from the ESP32 gateway (was %s)", wanted, entity_id)
        registry.async_update_entity(entity_id, new_entity_id=wanted)


def _recent_states(hass: HomeAssistant, entity_id: str, limit: int):
    """The newest `limit` recorded states of an entity, newest first. Recorder thread only."""
    from homeassistant.components.recorder.history import state_changes_during_period

    found = state_changes_during_period(
        hass,
        dt_util.utcnow() - timedelta(days=GATEWAY_HISTORY_DAYS),
        entity_id=entity_id,
        no_attributes=True,
        descending=True,
        limit=limit,
        include_start_time_state=False,
    )
    # `descending` only decides which end of the window the limit keeps; the recorder
    # hands the states back oldest first regardless. Sort here, so callers can rely on
    # newest first. (Seen in the field: the carry-on took a week-old total.)
    return sorted(found.get(entity_id, []), key=lambda state: state.last_updated, reverse=True)


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

    def _last(entity_id: str) -> tuple[str, datetime] | None:
        # The newest state that was an actual value, skipping unavailable/unknown.
        for state in _recent_states(hass, entity_id, 20):
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
# How far back to look for a gateway's recorded states. Newest first with a limit, so the
# recorder can use its index rather than scan the table.
GATEWAY_HISTORY_DAYS = 7


@dataclass(slots=True)
class GatewayRates:
    """The gateway's settings, worked out from what it recorded."""

    pulses_per_kwh: float | None = None
    cost_per_kwh: float | None = None


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
    entity_ids = gateway.readable
    if "last_pulses" not in entity_ids or "last_watt_hours" not in entity_ids:
        return GatewayRates()
    if "recorder" not in hass.config.components:
        return GatewayRates()

    from homeassistant.components.recorder import get_instance

    def _numbers(entity_id: str | None) -> dict[int, float]:
        """Recent numeric states, keyed by the second they were recorded."""
        if entity_id is None:
            return {}
        values: dict[int, float] = {}
        for state in _recent_states(hass, entity_id, RATE_HISTORY_STATES):
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
