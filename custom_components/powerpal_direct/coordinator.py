"""Keeps a Powerpal connected (or visits it periodically) and turns its records into data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import time
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

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
    STORAGE_VERSION,
)
from .energy import EnergyAccumulator, EnergyState
from .protocol import RECORD_INTERVAL_S, Measurement, PowerpalProtocolError

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
# The Powerpal sends no readings until its clock is set, and a new bond can clear it. The
# ESP32 gateway set it on connect and every 6 hours, and so does this.
CLOCK_SYNC_S = 6 * 3600
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
        # Off releases the Powerpal (e.g. for the phone app) until switched back on. Like the
        # gateway's connection switch, it's back on after a restart.
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

    @property
    def last_watt_hours(self) -> int | None:
        """Energy in the latest reading, rounded to whole Wh as the gateway did."""
        if self.data.last_pulses is None:
            return None
        return round(self.data.last_pulses / self.pulses_per_kwh * 1000)

    @property
    def last_cost(self) -> float | None:
        if self.data.last_pulses is None:
            return None
        return round(self.data.last_pulses / self.pulses_per_kwh * self.cost_per_kwh, 6)

    @property
    def power_available(self) -> bool:
        return (
            self.data.power_w is not None
            and self.data.power_updated is not None
            and time.monotonic() - self.data.power_updated <= self.power_valid_for_s
        )

    async def async_start(self) -> None:
        self.energy.state = EnergyState.from_dict(await self._store.async_load())
        if self.energy.roll_day(dt_util.now().date()):
            self._save()

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
        self._wake.set()
        if not enabled and self._client is not None:
            await self._client.disconnect()
        self.async_update_listeners()

    async def async_reconnect(self) -> None:
        """Drop the connection (if any) and connect again now."""
        self._wake.set()
        if self._client is not None:
            await self._client.disconnect()

    async def async_set_device_time(self) -> None:
        if self._client is None or not self._client.is_connected:
            raise HomeAssistantError("The Powerpal isn't connected right now")
        try:
            await self._client.set_time(int(time.time()))
        except (BleakError, TimeoutError) as err:
            raise HomeAssistantError(f"Couldn't set the Powerpal's clock: {err}") from err

    @callback
    def _async_on_advertisement(
        self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange
    ) -> None:
        # A connected Powerpal stops advertising, so any advertisement means it's free.
        self._advertised.set()

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
    def _set_connected(self, connected: bool) -> None:
        if self.data.connected != connected:
            self.data.connected = connected
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
        await client.set_time(int(time.time()))
        last_clock_sync = time.monotonic()
        if self.persistent:
            await client.ensure_reading_batch_size(self.batch_minutes)
        await client.subscribe(self._on_measurement, self._on_battery)
        self.data.identity = await client.read_identity(api_key)
        self._update_device_registry(self.data.identity)
        self.data.battery = await client.read_battery()
        self.data.led_sensitivity = await client.read_led_sensitivity()
        self._set_connected(True)
        self.async_update_listeners()
        # Staleness is judged per connection; catch-up covers whatever came before.
        self._last_record_arrival = time.monotonic()

        await self._async_catch_up(client, None)
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
                return
            if time.monotonic() - last_clock_sync > CLOCK_SYNC_S:
                last_clock_sync = time.monotonic()
                await client.set_time(int(time.time()))
            if time.monotonic() - last_battery > BATTERY_REFRESH_S:
                last_battery = time.monotonic()
                if (battery := await client.read_battery()) is not None:
                    self._on_battery(battery)
        _LOGGER.debug("%s: Disconnected", self.address)

    async def _async_catch_up(self, client: PowerpalClient, window: tuple[int, int] | None) -> None:
        """Replay stored records the totals haven't counted yet.

        With no window, everything from the watermark to the newest stored record is
        requested. The Powerpal clamps a start older than its oldest record, so a long
        outage needs no special handling beyond noting what was lost.
        """
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
            # First connection ever: start counting from now, rather than importing however
            # much history the Powerpal happens to hold into a brand new total.
            self.energy.set_watermark(history.newest)
            self._save()
            _LOGGER.info(
                "%s: Counting energy from %s onwards", self.address, dt_util.utc_from_timestamp(history.newest)
            )
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

        self._history_window = (start, end)
        self._history_records = []
        self._history_done.clear()
        self._history_last_arrival = time.monotonic()
        overall_s = 30 + expected / HISTORY_MIN_RATE
        _LOGGER.debug("%s: Requesting %s stored records (%s to %s)", self.address, expected, start, end)
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
            live = sorted(self._pending_live, key=lambda m: m.timestamp)
            self._history_window = None
            self._history_records = []
            self._pending_live = []
            counted = self._count(records)
            self._count(live)

        self.data.last_catch_up = CatchUpResult(
            requested_start=start,
            requested_end=end,
            expected=expected,
            received=len(records),
            counted=counted,
            unrecoverable_minutes=unrecoverable,
            finished_at=dt_util.utcnow(),
        )
        level = logging.INFO if len(records) >= expected else logging.WARNING
        _LOGGER.log(level, "%s: Caught up %s of %s stored readings", self.address, len(records), expected)

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
        self.data.last_record = dt_util.utc_from_timestamp(measurement.timestamp)
        self._save()
        self.async_update_listeners()
        return counted
