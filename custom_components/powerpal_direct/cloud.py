# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Optional upload of readings to Powerpal's cloud, for when Home Assistant replaces the phone.

While Home Assistant holds the Powerpal's connection, the Powerpal app can't connect, so
nothing reaches Powerpal's cloud. This sends each reading there instead, in the format the
community's REST upload has used: a JSON list of readings, posted to the device's
meter_reading endpoint with the Powerpal's own API key.

Off by default: it shares your usage with Powerpal. Readings are queued (and kept across
restarts) while the upload can't get through, and the queue is dropped when it's turned
off, so nothing is sent afterwards.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from http import HTTPStatus
import logging
import time
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

URL = "https://readings.powerpal.net/api/v1/meter_reading/{device_id}"
BATCH_SIZE = 60
# A week of one-minute readings. Beyond that the oldest are dropped.
MAX_PENDING = 7 * 24 * 60
REQUEST_TIMEOUT_S = 30
RETRY_MIN_S = 60
RETRY_MAX_S = 3600


class CloudUploader:
    """Queues readings and posts them to Powerpal's cloud while enabled."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cloud")
        self._lock = asyncio.Lock()
        self._pending: list[dict[str, Any]] = []
        self._retry_s = RETRY_MIN_S
        self._retry_at = 0.0
        self.enabled = False
        self.last_upload: datetime | None = None
        self.last_error: str | None = None

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def async_load(self) -> None:
        if data := await self._store.async_load():
            self._pending = list(data.get("pending", []))

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"pending": self._pending}, 10)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled and self._pending:
            # Turning it off means nothing more goes to Powerpal, including what was queued.
            self._pending.clear()
            self._save()

    def add(self, timestamp: int, pulses: int, watt_hours: int, cost: float) -> None:
        if not self.enabled:
            return
        self._pending.append(
            {"cost": cost, "is_peak": False, "pulses": pulses, "timestamp": timestamp, "watt_hours": watt_hours}
        )
        if len(self._pending) > MAX_PENDING:
            del self._pending[: len(self._pending) - MAX_PENDING]
        self._save()

    async def async_flush(self, device_id: str | None, api_key: str | None) -> None:
        """Send whatever is queued, a batch at a time, unless waiting to retry."""
        if not self.enabled or not self._pending or not device_id or not api_key:
            return
        if time.monotonic() < self._retry_at or self._lock.locked():
            return
        async with self._lock:
            while self.enabled and self._pending:
                batch = self._pending[:BATCH_SIZE]
                if not await self._post(device_id, api_key, batch):
                    return
                del self._pending[: len(batch)]
                self._save()

    async def _post(self, device_id: str, api_key: str, batch: list[dict[str, Any]]) -> bool:
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                URL.format(device_id=device_id),
                json=batch,
                headers={"authorization": api_key, "accept": ""},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as response:
                if response.status < HTTPStatus.BAD_REQUEST:
                    self.last_upload = dt_util.utcnow()
                    self.last_error = None
                    self._retry_s = RETRY_MIN_S
                    return True
                status = response.status
                body = (await response.text())[:200]
        except (aiohttp.ClientError, TimeoutError) as err:
            self._fail(f"{type(err).__name__}: {err}")
            return False

        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            # Wrong or revoked key: keep the readings, but don't hammer the server.
            self._fail(f"Powerpal refused the API key (HTTP {status})", retry_s=RETRY_MAX_S)
            return False
        if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            self._fail(f"HTTP {status}: {body}")
            return False
        # Any other refusal is about the readings themselves; retrying won't change that.
        _LOGGER.warning("Powerpal's cloud rejected %s readings (HTTP %s: %s); dropping them", len(batch), status, body)
        self.last_error = f"HTTP {status}: {body}"
        return True

    def _fail(self, error: str, retry_s: float | None = None) -> None:
        if error != self.last_error:
            _LOGGER.warning("Couldn't upload readings to Powerpal's cloud, will retry: %s", error)
        self.last_error = error
        delay = retry_s if retry_s is not None else self._retry_s
        self._retry_at = time.monotonic() + delay
        self._retry_s = min(self._retry_s * 2, RETRY_MAX_S)
