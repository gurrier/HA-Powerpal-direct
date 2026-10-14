"""Uploading readings to Powerpal's cloud."""

from __future__ import annotations

import json

from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant

from custom_components.powerpal_direct.cloud import BATCH_SIZE, CloudUploader

from .fakes import API_KEY, DEVICE_ID, T0

URL = f"https://readings.powerpal.net/api/v1/meter_reading/{DEVICE_ID}"


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
    await uploader.async_flush(DEVICE_ID, API_KEY)

    assert aioclient_mock.call_count == 1
    assert sent(aioclient_mock)[0] == [
        {"cost": 0.0015, "is_peak": False, "pulses": 5, "timestamp": T0, "watt_hours": 5},
        {"cost": 0.0021, "is_peak": False, "pulses": 7, "timestamp": T0 + 60, "watt_hours": 7},
    ]
    assert aioclient_mock.mock_calls[0][3]["authorization"] == API_KEY
    assert uploader.pending == 0
    assert uploader.last_upload is not None


async def test_a_backlog_goes_in_batches(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, status=200)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    for minute in range(BATCH_SIZE + 5):
        uploader.add(T0 + minute * 60, 1, 1, 0.0)
    await uploader.async_flush(DEVICE_ID, API_KEY)
    assert [len(batch) for batch in sent(aioclient_mock)] == [BATCH_SIZE, 5]


async def test_server_trouble_keeps_the_readings_for_later(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, status=503)
    uploader = CloudUploader(hass, "entry")
    uploader.set_enabled(True)
    uploader.add(T0, 5, 5, 0.0)
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
