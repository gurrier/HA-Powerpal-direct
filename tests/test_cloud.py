# SPDX-FileCopyrightText: 2026 gurrier <https://github.com/gurrier>
# SPDX-License-Identifier: GPL-3.0-only
"""Uploading readings to Powerpal's cloud."""

from __future__ import annotations

from contextlib import AbstractContextManager
import json
from unittest.mock import patch

from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant

from custom_components.powerpal_direct.cloud import MAX_BATCH, WINDOW_S, WINDOW_SETTLE_S, CloudUploader

from .fakes import API_KEY, DEVICE_ID, T0

URL = f"https://readings.powerpal.net/api/v1/meter_reading/{DEVICE_ID}"
# A half-hour window after the readings used here, since the server takes one request
# per window and only readings newer than what it holds.
WINDOW = (T0 // WINDOW_S + 2) * WINDOW_S


def at(seconds_into_window: int, windows_later: int = 0) -> AbstractContextManager:
    """Pretend it's that far into the window."""
    return patch(
        "custom_components.powerpal_direct.cloud.time.time",
        return_value=WINDOW + windows_later * WINDOW_S + seconds_into_window,
    )


def sent(aioclient_mock: AiohttpClientMocker) -> list[list[dict]]:
    return [json.loads(call[2]) if isinstance(call[2], (str, bytes)) else call[2] for call in aioclient_mock.mock_calls]


async def test_off_by_default_and_sends_nothing(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    uploader = CloudUploader(hass, "entry")
    uploader.add(T0, 5, 5, 0.0)
    await uploader.async_flush(DEVICE_ID, API_KEY)
    assert uploader.pending == 0
    assert aioclient_mock.call_count == 0


async def test_posts_readings_with_their_own_timestamps(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, status=200)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0015)
    uploader.add(T0 + 60, 7, 7, 0.0021)
    with at(WINDOW_SETTLE_S + 1):
        await uploader.async_flush(DEVICE_ID, API_KEY)

    assert aioclient_mock.call_count == 1
    assert sent(aioclient_mock)[0] == [
        {"cost": 0.0015, "is_peak": False, "pulses": 5, "timestamp": T0, "watt_hours": 5},
        {"cost": 0.0021, "is_peak": False, "pulses": 7, "timestamp": T0 + 60, "watt_hours": 7},
    ]
    assert aioclient_mock.mock_calls[0][3]["authorization"] == API_KEY
    assert uploader.pending == 0
    assert uploader.last_upload is not None


async def test_one_request_per_half_hour_window_with_everything_pending(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, status=200)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    for minute in range(MAX_BATCH + 5):
        uploader.add(T0 + minute * 60, 1, 1, 0.0)
    with at(WINDOW_SETTLE_S + 1):
        await uploader.async_flush(DEVICE_ID, API_KEY)
        # A second request in the same window would be thrown away by the server.
        await uploader.async_flush(DEVICE_ID, API_KEY)
    assert [len(batch) for batch in sent(aioclient_mock)] == [MAX_BATCH]
    assert uploader.pending == 5
    with at(WINDOW_SETTLE_S + 1, windows_later=1):
        await uploader.async_flush(DEVICE_ID, API_KEY)
    assert [len(batch) for batch in sent(aioclient_mock)] == [MAX_BATCH, 5]
    # Oldest first, so nothing is ever older than what the server already holds.
    first = sent(aioclient_mock)[0]
    assert [r["timestamp"] for r in first] == sorted(r["timestamp"] for r in first)


async def test_waits_a_minute_into_the_window(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The previous window's last reading arrives a few seconds past the half hour."""
    aioclient_mock.post(URL, status=200)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
    with at(10):
        await uploader.async_flush(DEVICE_ID, API_KEY)
    assert aioclient_mock.call_count == 0
    with at(WINDOW_SETTLE_S):
        await uploader.async_flush(DEVICE_ID, API_KEY)
    assert aioclient_mock.call_count == 1


async def test_a_restart_inside_the_window_does_not_send_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, status=200)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
    with at(WINDOW_SETTLE_S + 1):
        await uploader.async_flush(DEVICE_ID, API_KEY)
    await uploader._store.async_save({"pending": [], "last_window": uploader._last_window})

    restarted = CloudUploader(hass, "entry")
    await restarted.async_load()
    restarted.set_enabled(True)
    restarted.add(T0 + 60, 5, 5, 0.0)
    with at(WINDOW_SETTLE_S + 5):
        await restarted.async_flush(DEVICE_ID, API_KEY)
    assert aioclient_mock.call_count == 1
    with at(WINDOW_SETTLE_S + 5, windows_later=1):
        await restarted.async_flush(DEVICE_ID, API_KEY)
    assert aioclient_mock.call_count == 2


async def test_server_trouble_keeps_the_readings_for_later(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, status=503)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
    with at(WINDOW_SETTLE_S + 1):
        await uploader.async_flush(DEVICE_ID, API_KEY)
        assert uploader.pending == 1
        assert uploader.last_error is not None and "503" in uploader.last_error

        # Waiting out the retry delay, not hammering the server.
        await uploader.async_flush(DEVICE_ID, API_KEY)
    assert aioclient_mock.call_count == 1


async def test_turning_off_drops_what_was_queued(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, status=503)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
    with at(WINDOW_SETTLE_S + 1):
        await uploader.async_flush(DEVICE_ID, API_KEY)
    uploader.set_enabled(False)
    assert uploader.pending == 0


async def test_queue_survives_a_restart(hass: HomeAssistant, hass_storage: dict) -> None:
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
    await uploader._store.async_save({"pending": uploader._pending})

    restarted = CloudUploader(hass, "entry")
    await restarted.async_load()
    assert restarted.pending == 1
