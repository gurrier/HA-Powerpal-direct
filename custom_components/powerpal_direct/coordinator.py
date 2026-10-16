"""Keeps a Powerpal connected (or visits it periodically) and turns its records into data."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import statistics
import time
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .client import DeviceIdentity, PowerpalAuthError, PowerpalBondError, PowerpalClient
from .cloud import CloudUploader
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
    STORAGE_VERSION,
)
from .energy import EnergyAccumulator, EnergyState
from .gateway import GatewayEntities, async_last_gateway_readings
from .hourly import HourlyEnergyStatistic
from .protocol import RECORD_INTERVAL_S, HistoryRange, Measurement, PowerpalProtocolError

_LOGGER = logging.getLogger(__name__)

type PowerpalConfigEntry = ConfigEntry[PowerpalCoordinator]

RETRY_MIN_S = 10
RETRY_MAX_S = 300
# After an authentication failure, retrying sooner just repeats it.
AUTH_RETRY_S = 1800
# How long to wait for an advertisement before checking the cache again anyway.
ADVERTISEMENT_WAIT_S = 300
# A persistent connection that has gone this much longer than the batch size without a
# reading is treated as dead and reconnected. Catch-up then fills the gap.
STALE_GRACE_S = 150
BATTERY_REFRESH_S = 3600
# The Powerpal sends no readings until its clock is set, and a new bond can clear it. Rather
# than set it blindly (the ESP32 gateway did every 6 hours), it's set when that's needed:
# - on connect, when the Powerpal's history is empty, as it is after a reset;
# - when a connection gets no reading this long past the reading interval;
# - when the clock has drifted further than this from Home Assistant's, judged by when
#   live readings arrive, but no more often than every CLOCK_MIN_INTERVAL_S.
CLOCK_NO_READING_GRACE_S = 90
CLOCK_DRIFT_LIMIT_S = 60
CLOCK_MIN_INTERVAL_S = 6 * 3600
CLOCK_OFFSET_SAMPLES = 10
CLOCK_OFFSET_MIN_SAMPLES = 5
# A live reading only says something about the clock if it came a normal minute after the
# previous one. Readings arriving in a burst (stored ones, sent on reconnecting) are late
# by however long the connection was down, which says nothing about the clock.
CLOCK_SAMPLE_MIN_SPACING_S = 45
# The link often drops once within seconds of a new connection (seen on Windows and on
# Home Assistant); that isn't a stall worth reporting.
POST_CONNECT_DROP_S = 30
# Beyond this, averaging pulses over the gap wouldn't say anything about current power.
MAX_POWER_INTERVAL_S = 300
# History replies arrive at 20-70 records/s; give up on a burst that goes quiet this long.
HISTORY_IDLE_TIMEOUT_S = 15
HISTORY_MIN_RATE = 10  # records/s, for the overall timeout


@dataclass(slots=True)
class CatchUpResult:
    """How the last history catch-up went, for diagnostics."""

    requested_start: int
    requested_end: int
    expected: int
    received: int
    counted: int
    unrecoverable_minutes: int
    finished_at: datetime


@dataclass(slots=True)
class PowerpalData:
    """Everything the entities show."""

    connected: bool = False
    identity: DeviceIdentity | None = None
    battery: int | None = None
    led_sensitivity: int | None = None
    power_w: float | None = None
    # time.monotonic() when power_w was last set.
    power_updated: float | None = None
    last_pulses: int | None = None
    last_record: datetime | None = None
    history_oldest: datetime | None = None
    last_catch_up: CatchUpResult | None = None
    last_error: str | None = None
    # Heard only while the Powerpal is free, since it stops advertising once connected.
    advertisement_rssi: int | None = None
    advertisement_rate: int | None = None
    last_stall: str | None = None
    # The adapter or proxy holding the connection, e.g. "hci0 (18:1D:EA:F5:66:6B)".
    connected_via: str | None = None
    # Seconds the Powerpal's clock is behind Home Assistant's (negative: ahead).
    clock_offset: float | None = None


class PowerpalCoordinator(DataUpdateCoordinator[PowerpalData]):
    """Owns the connection to one Powerpal. Data is pushed, never polled by Home Assistant."""

    config_entry: PowerpalConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PowerpalConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title)
        self.address: str = entry.unique_id  # type: ignore[assignment]
        self._pairing_code: int = entry.data[CONF_PAIRING_CODE]
        self.pulses_per_kwh: float = entry.options.get(CONF_PULSES_PER_KWH, DEFAULT_PULSES_PER_KWH)
        self.batch_minutes: int = entry.options.get(CONF_NOTIFICATION_INTERVAL, DEFAULT_NOTIFICATION_INTERVAL)
        self.poll_minutes: int = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        self.cost_per_kwh: float = entry.options.get(CONF_COST_PER_KWH, DEFAULT_COST_PER_KWH)

        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.energy = EnergyAccumulator(EnergyState(), dt_util.get_default_time_zone())
        self.data = PowerpalData()

        self._client: PowerpalClient | None = None
        self._advertised = asyncio.Event()
        self._wake = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._catch_up_requested = asyncio.Event()
        # While a catch-up is running: the window being replayed, the records received in
        # it, and live readings that arrived meanwhile (counted after it, in order).
        self._history_window: tuple[int, int] | None = None
        self._history_records: list[Measurement] = []
        self._history_done = asyncio.Event()
        self._history_last_arrival = 0.0
        self._pending_live: list[Measurement] = []
        self._last_record_arrival: float | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._stopping = False
        self.first_setup = False
        self.gateway = GatewayEntities(available={}, still_present={})
        self.cloud = CloudUploader(hass, entry.entry_id)
        self.hourly = HourlyEnergyStatistic(hass, entry.entry_id, self.address, entry.title)
        self._clock_offsets: deque[float] = deque(maxlen=CLOCK_OFFSET_SAMPLES)
        self._clock_set_at: float | None = None
        self._clock_correction_due = False
        self._last_live_arrival: float | None = None
        self._connected_at: float | None = None
        # Why the last connection was lost, and when its last reading arrived (wall time),
        # reported as Last Stall once readings resume.
        self._drop_reason: str | None = None
        self._drop_since: float | None = None
        self._last_record_wall: float | None = None
        self._intentional_drop = False
        # Off releases the Powerpal (e.g. for the phone app or an ESP32 gateway) until
        # switched back on. The switch entity restores it across restarts.
        self.connection_enabled = True
        self._warned_passive_only = False

    @property
    def persistent(self) -> bool:
        return self.poll_minutes == 0

    @property
    def power_valid_for_s(self) -> float:
        if self.persistent:
            return self.batch_minutes * 60 + STALE_GRACE_S
        return self.poll_minutes * 60 + STALE_GRACE_S

    @property
    def total_kwh(self) -> float:
        return self.energy.state.total_pulses / self.pulses_per_kwh

    @property
    def daily_kwh(self) -> float:
        return self.energy.state.daily_pulses / self.pulses_per_kwh

    def watt_hours(self, pulses: int) -> int:
        """Energy in one reading, rounded to whole Wh as the gateway did."""
        return round(pulses / self.pulses_per_kwh * 1000)

    def cost(self, pulses: int) -> float:
        return round(pulses / self.pulses_per_kwh * self.cost_per_kwh, 6)

    @property
    def last_watt_hours(self) -> int | None:
        return None if self.data.last_pulses is None else self.watt_hours(self.data.last_pulses)

    @property
    def last_cost(self) -> float | None:
        return None if self.data.last_pulses is None else self.cost(self.data.last_pulses)

    @property
    def power_available(self) -> bool:
        return (
            self.data.power_w is not None
            and self.data.power_updated is not None
            and time.monotonic() - self.data.power_updated <= self.power_valid_for_s
        )

    async def async_load(self) -> None:
        """Load the saved totals. Must run before the entities are added.

        Otherwise Total Energy is first written as 0 and then jumps back to the real
        total: Home Assistant's statistics take the drop as a meter reset and the jump as
        consumption, and the Energy dashboard's cost sensor, which starts afresh at every
        restart from the first value it sees, would charge the whole jump at today's price.
        """
        saved = await self._store.async_load()
        # Nothing saved yet: this config entry is being set up for the first time.
        self.first_setup = saved is None
        self.energy.state = EnergyState.from_dict(saved)
        await self._async_carry_on_from_gateway()
        if self.energy.roll_day(dt_util.now().date()):
            self._save()

    async def async_start(self) -> None:
        await self.hourly.async_load()
        await self.hourly.async_seed(
            er.async_get(self.hass).async_get_entity_id("sensor", DOMAIN, f"{self.address}_energy")
        )
        await self.cloud.async_load()
        if (manager := _bluetooth_manager()) is not None:
            self._unsubs.append(manager.async_register_allocation_callback(self._async_on_allocations))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_retry_cloud_upload, timedelta(minutes=5)))
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_sample_advertisement_rate, timedelta(minutes=1))
        )

        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self._unsubs.append(async_track_time_change(self.hass, self._async_midnight, hour=0, minute=0, second=0))
        self.config_entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} {self.address} connection"
        )

    async def async_stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._client is not None:
            await self._client.disconnect()
        await self._store.async_save(self.energy.state.as_dict())

    async def async_set_connection_enabled(self, enabled: bool) -> None:
        """Hold the Powerpal's connection, or let go of it and stay away."""
        self.connection_enabled = enabled
        self._intentional_drop = True
        self._wake.set()
        if not enabled and self._client is not None:
            await self._client.disconnect()
        self.async_update_listeners()

    async def async_reconnect(self) -> None:
        """Drop the connection (if any) and connect again now."""
        self._intentional_drop = True
        self._wake.set()
        if self._client is not None:
            await self._client.disconnect()

    async def async_set_device_time(self) -> None:
        if self._client is None or not self._client.is_connected:
            raise HomeAssistantError("The Powerpal isn't connected right now")
        try:
            await self._async_set_clock(self._client, "requested")
        except (BleakError, TimeoutError) as err:
            raise HomeAssistantError(f"Couldn't set the Powerpal's clock: {err}") from err

    @callback
    def _async_on_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        # A connected Powerpal stops advertising, so any advertisement means it's free.
        self._advertised.set()
        if not self.data.connected and service_info.rssi != self.data.advertisement_rssi:
            self.data.advertisement_rssi = service_info.rssi
            self.async_update_listeners()

    @callback
    def _async_sample_advertisement_rate(self, now: datetime) -> None:
        """Advertisements per minute, as the gateway reported: non-zero means the link was down.

        Home Assistant only passes on advertisements that change, so they can't be counted.
        It does track when each device was last heard, and learns how often it advertises.
        """
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=False)
        if info is None or time.monotonic() - info.time > 60:
            rate: int | None = 0
        elif interval := bluetooth.async_get_learned_advertising_interval(self.hass, self.address):
            rate = round(60 / interval)
        else:
            rate = None  # heard, but not long enough to know how often
        if rate != self.data.advertisement_rate:
            self.data.advertisement_rate = rate
            self.async_update_listeners()

    async def _async_carry_on_from_gateway(self) -> None:
        """On first setup after an ESP32 gateway, start from where it left off.

        Its last Total Energy becomes the starting total, so the energy sensor carries on
        rather than dropping to zero, and its last Timestamp becomes the watermark, so the
        first catch-up fetches the minutes since the gateway stopped from the Powerpal's
        own history.
        """
        state = self.energy.state
        if state.total_pulses or state.watermark is not None or not self.gateway.available:
            return
        readings = await async_last_gateway_readings(self.hass, self.gateway.available)
        if readings.total_kwh is not None:
            state.total_pulses = round(readings.total_kwh * self.pulses_per_kwh)
        if readings.daily_kwh is not None and readings.daily_date == dt_util.now().date().isoformat():
            state.daily_pulses = round(readings.daily_kwh * self.pulses_per_kwh)
            state.daily_date = readings.daily_date
        if readings.last_reading is not None:
            state.watermark = readings.last_reading
        if readings.total_kwh is not None or readings.last_reading is not None:
            _LOGGER.info(
                "%s: Carrying on from the ESP32 gateway: total %s kWh, last reading %s",
                self.address,
                readings.total_kwh,
                dt_util.utc_from_timestamp(readings.last_reading) if readings.last_reading else None,
            )
            self._save()

    @callback
    def async_set_cloud_upload(self, enabled: bool) -> None:
        self.cloud.set_enabled(enabled)
        self.async_update_listeners()

    @callback
    def _async_retry_cloud_upload(self, now: datetime) -> None:
        self._schedule_cloud_upload()

    @callback
    def _schedule_cloud_upload(self) -> None:
        if not self.cloud.enabled or not self.cloud.pending or (identity := self.data.identity) is None:
            return
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_cloud_upload(identity.device_id, identity.api_key),
            f"{DOMAIN} {self.address} cloud upload",
        )

    async def _async_cloud_upload(self, device_id: str, api_key: str) -> None:
        await self.cloud.async_flush(device_id, api_key)
        self.async_update_listeners()

    @callback
    def _async_midnight(self, now: datetime) -> None:
        if self.energy.roll_day(now.date()):
            self._save()
            self.async_update_listeners()

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self.energy.state.as_dict, 10)

    @callback
    def _update_device_registry(self, identity: DeviceIdentity) -> None:
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            registry.async_update_device(
                device.id,
                serial_number=identity.device_id,
                model=identity.model,
                sw_version=identity.firmware,
                hw_version=identity.hardware,
            )

    @property
    def _bond_issue_id(self) -> str:
        return f"bond_failed_{self.config_entry.entry_id}"

    @callback
    def _warn_if_passive_only(self) -> None:
        """Explain the one case where waiting won't help: nothing can connect to it."""
        if self._warned_passive_only:
            return
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=False)
        if info is None or info.connectable:
            return
        self._warned_passive_only = True
        _LOGGER.warning(
            "%s: The Powerpal is only heard by %s, which can't make connections. It needs a "
            "Bluetooth adapter or an ESPHome Bluetooth proxy with active: true in range",
            self.address,
            info.source,
        )

    @callback
    def _note_drop(self) -> None:
        """Remember why a live connection ended, unless it was meant to."""
        intentional, self._intentional_drop = self._intentional_drop, False
        if not self.data.connected or not self.persistent or self._stopping or intentional:
            return
        if (
            self._drop_reason is None
            and self._connected_at is not None
            and time.monotonic() - self._connected_at < POST_CONNECT_DROP_S
        ):
            _LOGGER.debug("%s: Link dropped just after connecting; not counted as a stall", self.address)
            return
        if self._drop_reason is None:
            self._drop_reason = f"disconnected: {self.data.last_error}" if self.data.last_error else "the link dropped"
        if self._drop_since is None:
            self._drop_since = self._last_record_wall or time.time()

    @callback
    def _report_recovered_stall(self) -> None:
        """After reconnecting from an unplanned drop, say what happened, as Last Stall."""
        if self._drop_reason is None:
            return
        gap_s = time.time() - (self._drop_since or time.time())
        text = f"{_describe_gap(gap_s)} gap: {self._drop_reason}"
        if (catch_up := self.data.last_catch_up) is not None and catch_up.finished_at > dt_util.utcnow() - timedelta(
            minutes=1
        ):
            text += f"; recovered {catch_up.received} of {catch_up.expected} readings from the Powerpal"
        self.data.last_stall = text[:255]
        self._drop_reason = None
        self._drop_since = None
        self.async_update_listeners()

    @callback
    def _async_on_allocations(self, allocations: Any) -> None:
        if self.data.connected and self._update_connected_via():
            self.async_update_listeners()

    @callback
    def _update_connected_via(self) -> bool:
        """Find which adapter or proxy holds our connection. Returns whether it changed.

        Home Assistant tracks which device addresses hold a connection slot on each
        adapter and proxy, which is the one public record of the path a connection took.
        A proxy reports its slots a moment after connecting, hence the callback too.
        """
        via = None
        if self.data.connected and (manager := _bluetooth_manager()) is not None:
            for allocations in manager.async_current_allocations() or []:
                if self.address.upper() in (address.upper() for address in allocations.allocated):
                    scanner = bluetooth.async_scanner_by_source(self.hass, allocations.source)
                    via = scanner.name if scanner is not None else allocations.source
                    break
        if via == self.data.connected_via:
            return False
        if via is not None and self.data.connected_via is not None:
            _LOGGER.info("%s: Connection moved from %s to %s", self.address, self.data.connected_via, via)
        self.data.connected_via = via
        return True

    @callback
    def _set_connected(self, connected: bool) -> None:
        if self.data.connected != connected:
            self.data.connected = connected
            self._update_connected_via()
            self.async_update_listeners()

    async def _async_run(self) -> None:
        retry_s = RETRY_MIN_S
        while not self._stopping:
            if not self.connection_enabled:
                self._set_connected(False)
                await self._wake.wait()
                self._wake.clear()
                continue
            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
            if ble_device is None:
                self._set_connected(False)
                self._warn_if_passive_only()
                _LOGGER.debug("%s: Not advertising; waiting for it", self.address)
                self._advertised.clear()
                await self._wait_any((self._advertised, self._wake), ADVERTISEMENT_WAIT_S)
                self._wake.clear()
                continue

            delay: float
            try:
                await self._async_session(ble_device)
            except PowerpalAuthError as err:
                self.data.last_error = str(err)
                _LOGGER.error(
                    "%s: The Powerpal rejected the pairing code. Check it in the integration's settings: %s",
                    self.address,
                    err,
                )
                self.config_entry.async_start_reauth(self.hass)
                delay = AUTH_RETRY_S
            except PowerpalBondError as err:
                self.data.last_error = str(err)
                _LOGGER.error(
                    "%s: Couldn't bond with the Powerpal, so it won't answer. Connecting through an "
                    "ESPHome Bluetooth proxy is the most reliable way to bond: %s",
                    self.address,
                    err,
                )
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._bond_issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="bond_failed",
                    translation_placeholders={"name": self.config_entry.title, "error": str(err)},
                )
                delay = AUTH_RETRY_S
            except (BleakError, TimeoutError, PowerpalProtocolError) as err:
                self.data.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.debug("%s: Connection failed: %s", self.address, err)
                delay = retry_s
                retry_s = min(retry_s * 2, RETRY_MAX_S)
            except Exception as err:  # Keep the connection loop alive whatever happens.
                self.data.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.exception("%s: Unexpected error", self.address)
                delay = RETRY_MAX_S
            else:
                self.data.last_error = None
                retry_s = RETRY_MIN_S
                delay = 0 if self.persistent else self.poll_minutes * 60
            finally:
                client, self._client = self._client, None
                if client is not None:
                    await client.disconnect()
                self._history_window = None
                self._note_drop()
                self._set_connected(False)

            if delay:
                await self._wait_any((self._wake,), delay)
            self._wake.clear()

    async def _wait_any(self, events: tuple[asyncio.Event, ...], timeout: float) -> None:
        waiters = [asyncio.ensure_future(event.wait()) for event in events]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _async_session(self, ble_device: BLEDevice) -> None:
        """One connection: set up, catch up on history, then (if persistent) stream."""
        self._disconnected.clear()
        self._catch_up_requested.clear()
        # A switch-off or reconnect requested while nothing was connected would otherwise
        # make this session's eventual drop look intentional.
        self._intentional_drop = False
        # Anything held back at the end of the last session is in the Powerpal's history,
        # which catch-up fetches; counting it now would skip the gap check.
        self._pending_live = []
        client = PowerpalClient(
            ble_device,
            self._pairing_code,
            ble_device_callback=lambda: (
                bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True) or ble_device
            ),
            disconnected_callback=self._disconnected.set,
        )
        self._client = client
        await client.connect()
        _LOGGER.debug("%s: Connected", self.address)
        # The same order as the ESP32 gateway (esphome-powerpal_ble), which is proven to
        # hold a stable connection: pairing code, reading interval, subscribe, then reads.
        api_key = await client.authenticate()
        ir.async_delete_issue(self.hass, DOMAIN, self._bond_issue_id)
        clock_set_this_session = False
        history = await client.read_history_range()
        if history.newest == 0:
            await self._async_set_clock(client, "its history is empty, as after a reset")
            clock_set_this_session = True
        if self.persistent:
            await client.ensure_reading_batch_size(self.batch_minutes)
        await client.subscribe(self._on_measurement, self._on_battery)
        self.data.identity = await client.read_identity(api_key)
        self._update_device_registry(self.data.identity)
        self.data.battery = await client.read_battery()
        self.data.led_sensitivity = await client.read_led_sensitivity()
        self._set_connected(True)
        self.data.advertisement_rssi = None
        self.async_update_listeners()
        # Staleness is judged per connection; catch-up covers whatever came before.
        connected_at = time.monotonic()
        self._connected_at = connected_at
        self._last_record_arrival = connected_at
        self._clock_offsets.clear()
        self._last_live_arrival = None

        await self._async_catch_up(client, None, history)
        self._report_recovered_stall()
        if not self.persistent:
            return

        last_battery = time.monotonic()
        while client.is_connected and not self._disconnected.is_set():
            await self._wait_any((self._disconnected, self._catch_up_requested), 60)
            if self._disconnected.is_set():
                break
            if self._catch_up_requested.is_set():
                self._catch_up_requested.clear()
                if self._history_window is not None:
                    await self._async_catch_up(client, self._history_window)
            stale_after = self.batch_minutes * 60 + STALE_GRACE_S
            if self._last_record_arrival is not None and time.monotonic() - self._last_record_arrival > stale_after:
                _LOGGER.warning("%s: No reading for over %s s; reconnecting to recover", self.address, stale_after)
                self._drop_reason = "connected, but the Powerpal stopped sending readings"
                return
            silent_for = time.monotonic() - self._last_record_arrival
            if (
                not clock_set_this_session
                and self._last_record_arrival == connected_at
                and silent_for > self.batch_minutes * 60 + CLOCK_NO_READING_GRACE_S
            ):
                # No reading at all since connecting: an unset clock is the known cause.
                await self._async_set_clock(client, f"no reading in {round(silent_for)} s since connecting")
                clock_set_this_session = True
            if self._clock_correction_due:
                self._clock_correction_due = False
                offset = self.data.clock_offset or 0.0
                direction = "behind" if offset > 0 else "ahead of"
                await self._async_set_clock(client, f"it was {abs(offset):.0f} s {direction} Home Assistant's")
                # The Powerpal's timestamps move by the same amount, stored ones included.
                # Moving the watermark with them keeps the next reading a minute past it;
                # otherwise a clock set back would make new readings look already counted.
                self.energy.shift_watermark(round(offset))
                self._save()
            if time.monotonic() - last_battery > BATTERY_REFRESH_S:
                last_battery = time.monotonic()
                if (battery := await client.read_battery()) is not None:
                    self._on_battery(battery)
        _LOGGER.debug("%s: Disconnected", self.address)

    async def _async_catch_up(
        self, client: PowerpalClient, window: tuple[int, int] | None, history: HistoryRange | None = None
    ) -> None:
        """Replay stored records the totals haven't counted yet.

        With no window, everything from the watermark to the newest stored record is
        requested. The Powerpal clamps a start older than its oldest record, so a long
        outage needs no special handling beyond noting what was lost.
        """
        if history is None:
            history = await client.read_history_range()
        if history.newest == 0:
            # Nothing stored, as just after the Powerpal resets. Live readings still arrive,
            # and the first one starts the count.
            _LOGGER.debug("%s: The Powerpal holds no stored readings", self.address)
            self.data.history_oldest = None
            self._history_window = None
            self._flush_pending_live()
            return
        self.data.history_oldest = dt_util.utc_from_timestamp(history.oldest)
        watermark = self.energy.watermark
        if watermark is None:
            # First connection ever: the total starts from now, rather than taking in however
            # much history the Powerpal happens to hold. On a brand new install that history
            # still goes into the hourly statistic, so the Energy dashboard shows it.
            if self.hourly.available and not self.hourly.ledger.hours and not self.hourly.ledger.baseline_kwh:
                imported = await self._async_fetch_history(client, history.oldest, history.newest)
                for measurement in imported:
                    self.hourly.add(measurement.timestamp, measurement.pulses)
                self.hourly.flush(self.pulses_per_kwh)
                _LOGGER.info(
                    "%s: Imported %s stored readings into %s", self.address, len(imported), self.hourly.statistic_id
                )
            self.energy.set_watermark(history.newest)
            self._save()
            _LOGGER.info(
                "%s: Counting energy from %s onwards", self.address, dt_util.utc_from_timestamp(history.newest)
            )
            self._flush_pending_live()
            return

        if window is None:
            if history.newest <= watermark:
                return
            window = (watermark + 1, history.newest)
        start, end = window
        if start > end:
            self._history_window = None
            self._flush_pending_live()
            return
        unrecoverable = max(0, (history.oldest - start) // RECORD_INTERVAL_S)
        expected = max(0, (end - max(start, history.oldest)) // RECORD_INTERVAL_S + 1)
        if unrecoverable:
            _LOGGER.warning(
                "%s: %s minutes of readings are older than anything the Powerpal still holds and are lost",
                self.address,
                unrecoverable,
            )

        records = await self._async_fetch_history(client, start, end)
        counted = self._count(records)
        self._flush_pending_live()

        self.data.last_catch_up = CatchUpResult(
            requested_start=start,
            requested_end=end,
            expected=expected,
            received=len(records),
            counted=counted,
            unrecoverable_minutes=unrecoverable,
            finished_at=dt_util.utcnow(),
        )
        # On reconnecting, the Powerpal often sends the readings it couldn't deliver by
        # itself, before they're asked for. Only a gap that's still there is worth a warning.
        watermark = self.energy.watermark
        still_missing = watermark is None or watermark < end - RECORD_INTERVAL_S
        if len(records) >= expected or not still_missing:
            _LOGGER.info(
                "%s: Caught up: %s of %s readings replayed, the rest had already arrived",
                self.address,
                len(records),
                expected,
            )
        else:
            _LOGGER.warning("%s: Caught up only %s of %s stored readings", self.address, len(records), expected)

    async def _async_fetch_history(self, client: PowerpalClient, start: int, end: int) -> list[Measurement]:
        """Have the Powerpal replay its stored readings in [start, end], and collect them.

        Live readings that arrive meanwhile are held in _pending_live for the caller.
        """
        expected = max(0, (end - start) // RECORD_INTERVAL_S + 1)
        self._history_window = (start, end)
        self._history_records = []
        self._history_done.clear()
        self._history_last_arrival = time.monotonic()
        overall_s = 30 + expected / HISTORY_MIN_RATE
        _LOGGER.debug("%s: Requesting stored readings %s to %s", self.address, start, end)
        try:
            await client.request_history(start, end)
            deadline = time.monotonic() + overall_s
            while not self._history_done.is_set() and not self._disconnected.is_set():
                now = time.monotonic()
                if now > deadline or now - self._history_last_arrival > HISTORY_IDLE_TIMEOUT_S:
                    break
                await self._wait_any((self._history_done, self._disconnected), 1)
        finally:
            records = sorted(self._history_records, key=lambda m: m.timestamp)
            self._history_window = None
            self._history_records = []
        return records

    async def _async_set_clock(self, client: PowerpalClient, reason: str) -> None:
        _LOGGER.info("%s: Setting the Powerpal's clock: %s", self.address, reason)
        await client.set_time(int(time.time()))
        self._clock_set_at = time.monotonic()
        self._clock_offsets.clear()
        # Unknown again until enough readings have arrived under the new setting.
        self.data.clock_offset = None

    @callback
    def _note_clock_offset(self, measurement: Measurement) -> None:
        """Estimate the Powerpal's clock offset from when a live reading arrived.

        A reading covers the minute starting at its timestamp, and is sent as that minute
        ends, so it should arrive about 60 s after its timestamp (plus a second or two of
        delivery). Only meaningful with a one-minute interval.
        """
        if self.batch_minutes != 1:
            return
        arrival = time.time()
        previous, self._last_live_arrival = self._last_live_arrival, arrival
        if previous is None or arrival - previous < CLOCK_SAMPLE_MIN_SPACING_S:
            return
        self._clock_offsets.append(arrival - (measurement.timestamp + RECORD_INTERVAL_S))
        if len(self._clock_offsets) < CLOCK_OFFSET_MIN_SAMPLES:
            return
        offset = statistics.median(self._clock_offsets)
        self.data.clock_offset = round(offset, 1)
        recently_set = self._clock_set_at is not None and time.monotonic() - self._clock_set_at < CLOCK_MIN_INTERVAL_S
        if abs(offset) > CLOCK_DRIFT_LIMIT_S and not recently_set:
            self._clock_correction_due = True

    @callback
    def _flush_pending_live(self) -> None:
        live, self._pending_live = sorted(self._pending_live, key=lambda m: m.timestamp), []
        self._count(live)

    @callback
    def _on_battery(self, level: int) -> None:
        self.data.battery = level
        self.async_update_listeners()

    @callback
    def _on_measurement(self, measurement: Measurement) -> None:
        self._last_record_arrival = time.monotonic()
        if (window := self._history_window) is not None:
            if measurement.timestamp <= window[1]:
                self._history_records.append(measurement)
                self._history_last_arrival = time.monotonic()
                # Records sit on a one-minute grid, so the last one in the window is
                # within a minute of its end, whether or not the end is on the grid.
                if measurement.timestamp > window[1] - RECORD_INTERVAL_S:
                    self._history_done.set()
            else:
                self._pending_live.append(measurement)
            return

        self._note_clock_offset(measurement)
        watermark = self.energy.watermark
        expected_gap = self.batch_minutes * RECORD_INTERVAL_S
        if (
            watermark is not None
            and measurement.timestamp - watermark > expected_gap + RECORD_INTERVAL_S // 2
            and self._client is not None
        ):
            # Readings went missing on a connection that stayed up. Fetch them before
            # counting this one, or the watermark would pass over them for good.
            _LOGGER.debug("%s: Gap before reading at %s; catching up", self.address, measurement.timestamp)
            self._history_window = (watermark + 1, measurement.timestamp - 1)
            self._pending_live.append(measurement)
            self._catch_up_requested.set()
            return

        self._count([measurement])

    @callback
    def _count(self, records: list[Measurement]) -> int:
        """Add records to the totals, in order. Returns how many were new."""
        counted = 0
        newest = None
        for measurement in records:
            result = self.energy.add(measurement)
            if result is None:
                _LOGGER.debug("%s: Already counted reading at %s", self.address, measurement.timestamp)
                continue
            if result.clock_reset:
                _LOGGER.warning(
                    "%s: The Powerpal's clock went backwards (to %s); counting from there",
                    self.address,
                    dt_util.utc_from_timestamp(measurement.timestamp),
                )
            counted += 1
            newest = result
            self.hourly.add(measurement.timestamp, measurement.pulses)
            self.cloud.add(
                measurement.timestamp,
                measurement.pulses,
                self.watt_hours(measurement.pulses),
                self.cost(measurement.pulses),
            )
        if newest is None:
            return 0

        measurement = newest.measurement
        interval = newest.interval_s
        if interval is None or interval > MAX_POWER_INTERVAL_S:
            # A gap that wasn't backfilled. Records are always one minute each, so the
            # newest one alone still gives power, provided it isn't a multi-minute batch.
            interval = RECORD_INTERVAL_S if self.batch_minutes == 1 or not self.persistent else None
        if interval:
            self.data.power_w = round(measurement.pulses / self.pulses_per_kwh * 3_600_000 / interval, 1)
            self.data.power_updated = time.monotonic()
        self.data.last_pulses = measurement.pulses
        self._last_record_wall = time.time()
        self.data.last_record = dt_util.utc_from_timestamp(measurement.timestamp)
        self._save()
        self.hourly.flush(self.pulses_per_kwh)
        self._schedule_cloud_upload()
        self.async_update_listeners()
        return counted


def _describe_gap(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h"


def _bluetooth_manager():
    try:
        from habluetooth import get_manager

        return get_manager()
    except Exception:
        return None
