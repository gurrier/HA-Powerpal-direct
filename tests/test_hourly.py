"""The hourly energy statistic: every reading booked to the hour it was measured in."""

from __future__ import annotations

from datetime import datetime

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.powerpal_direct.hourly import HOUR_S, KEEP_HOURS, HourlyEnergyStatistic, HourlyLedger

H0 = 1_791_000_000 - 1_791_000_000 % HOUR_S


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock: object, enable_custom_integrations: None) -> None:
    """The recorder has to start before Home Assistant does, so ask for it first."""


def test_late_readings_land_in_their_own_hour() -> None:
    ledger = HourlyLedger(baseline_kwh=100.0)
    ledger.add(H0 + 600, 500)  # 0.5 kWh in the first hour
    ledger.add(H0 + HOUR_S + 60, 250)  # 0.25 kWh in the second
    assert ledger.rows(1000) == [(H0, 100.5), (H0 + HOUR_S, 100.75)]

    # Catch-up after an outage brings readings from the first hour, late.
    ledger.dirty_from = None
    ledger.add(H0 + 1200, 300)
    assert ledger.dirty_from == H0
    assert ledger.rows(1000, since=ledger.dirty_from) == [(H0, 100.8), (H0 + HOUR_S, 101.05)]


def test_old_hours_fold_into_the_baseline() -> None:
    ledger = HourlyLedger(baseline_kwh=10.0)
    ledger.add(H0, 1000)
    latest = H0 + (KEEP_HOURS + 1) * HOUR_S
    ledger.add(latest, 1000)
    ledger.prune(latest + 10, 1000)
    assert ledger.baseline_kwh == 11.0
    assert list(ledger.hours) == [latest]
    assert ledger.rows(1000) == [(latest, 12.0)]


def test_round_trips() -> None:
    ledger = HourlyLedger(baseline_kwh=1.5, hours={H0: 7})
    again = HourlyLedger.from_dict(ledger.as_dict())
    assert (again.baseline_kwh, again.hours) == (1.5, {H0: 7})


async def _hourly_sums(hass: HomeAssistant, statistic_id: str) -> list[tuple[datetime, float]]:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import statistics_during_period

    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utc_from_timestamp(0), None, {statistic_id}, "hour", None, {"sum"}
    )
    return [(dt_util.utc_from_timestamp(row["start"]), row["sum"]) for row in rows.get(statistic_id, [])]


async def test_writes_an_external_statistic(hass: HomeAssistant) -> None:
    statistic = HourlyEnergyStatistic(hass, "entry", "DF:5C:55:00:00:01", "Powerpal 00002ba7")
    assert statistic.statistic_id == "powerpal_direct:energy_df_5c_55_00_00_01"
    await statistic.async_seed(None)
    statistic.add(H0 + 60, 400)
    statistic.add(H0 + HOUR_S + 60, 100)
    statistic.flush(1000)
    assert await _hourly_sums(hass, statistic.statistic_id) == [
        (dt_util.utc_from_timestamp(H0), 0.4),
        (dt_util.utc_from_timestamp(H0 + HOUR_S), 0.5),
    ]

    # A reading recovered late rewrites its hour and every one after it.
    statistic.add(H0 + 120, 200)
    statistic.flush(1000)
    assert await _hourly_sums(hass, statistic.statistic_id) == [
        (dt_util.utc_from_timestamp(H0), 0.6),
        (dt_util.utc_from_timestamp(H0 + HOUR_S), 0.7),
    ]


async def test_seeded_from_the_energy_sensors_history(hass: HomeAssistant) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics

    # The energy sensor's existing hourly statistics, e.g. the ESP32 gateway's.
    sensor = "sensor.powerpal_gateway_powerpal_total_energy"
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": sensor,
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": dt_util.utc_from_timestamp(H0 - 2 * HOUR_S), "sum": 5000.0, "state": 5000.0},
            {"start": dt_util.utc_from_timestamp(H0 - HOUR_S), "sum": 5001.25, "state": 5001.25},
        ],
    )
    await async_wait_recording_done(hass)

    statistic = HourlyEnergyStatistic(hass, "entry", "DF:5C:55:00:00:01", "Powerpal 00002ba7")
    await statistic.async_seed(sensor)
    assert statistic.ledger.baseline_kwh == 5001.25
    statistic.add(H0 + 60, 750)
    statistic.flush(1000)
    assert await _hourly_sums(hass, statistic.statistic_id) == [
        (dt_util.utc_from_timestamp(H0 - 2 * HOUR_S), 5000.0),
        (dt_util.utc_from_timestamp(H0 - HOUR_S), 5001.25),
        (dt_util.utc_from_timestamp(H0), 5002.0),
    ]

    # Seeding only happens once.
    await statistic.async_seed(sensor)
    assert statistic.ledger.baseline_kwh == 5001.25


async def test_removing_the_entry_clears_the_statistic(hass: HomeAssistant) -> None:
    from custom_components.powerpal_direct.hourly import async_clear_hourly_statistic

    statistic = HourlyEnergyStatistic(hass, "entry", "DF:5C:55:00:00:01", "Powerpal 00002ba7")
    await statistic.async_seed(None)
    statistic.add(H0 + 60, 400)
    statistic.flush(1000)
    assert await _hourly_sums(hass, statistic.statistic_id)

    async_clear_hourly_statistic(hass, "DF:5C:55:00:00:01")
    assert await _hourly_sums(hass, statistic.statistic_id) == []
