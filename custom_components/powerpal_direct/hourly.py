"""An hourly energy statistic that books every reading to the hour it was measured in.

Home Assistant compiles a sensor's statistics from the moments its state changes, so
energy recovered by catch-up after an outage lands in the hour it was recovered, not the
hours it was used. Every Powerpal reading carries its own minute timestamp, so this keeps
an exact per-hour ledger and writes it as an external statistic, rewriting the hours that
late readings belong to. The Energy dashboard can use it in place of the sensor.

It's a separate statistic, so the energy sensor's own statistics (and anything built on
them, such as utility meters) are untouched. When it's first created it copies the hourly
history the energy sensor already has (after taking over from an ESP32 gateway, that's
the gateway's), so switching the Energy dashboard over leaves no gap.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

HOUR_S = 3600
# Hours kept individually, to rewrite when late readings arrive. Older hours are folded
# into the baseline; the Powerpal's own history is shorter than this anyway.
KEEP_HOURS = 31 * 24


def hour_of(timestamp: int) -> int:
    return timestamp - timestamp % HOUR_S


class HourlyLedger:
    """Pulses per hour, plus the energy total before the first of them.

    Pure bookkeeping, no Home Assistant, so the sums can be tested on their own.
    """

    def __init__(self, baseline_kwh: float | None = None, hours: dict[int, int] | None = None) -> None:
        # None until seeded: the total before the first hour in the ledger.
        self.baseline_kwh = baseline_kwh
        self.hours: dict[int, int] = dict(hours or {})
        # The earliest hour changed since the last write, if any.
        self.dirty_from: int | None = None

    def add(self, timestamp: int, pulses: int) -> None:
        hour = hour_of(timestamp)
        self.hours[hour] = self.hours.get(hour, 0) + pulses
        self.dirty_from = hour if self.dirty_from is None else min(self.dirty_from, hour)

    def rows(self, pulses_per_kwh: float, since: int | None = None) -> list[tuple[int, float]]:
        """(hour start, cumulative kWh at the end of that hour), from `since` onwards."""
        total = self.baseline_kwh or 0.0
        rows = []
        for hour in sorted(self.hours):
            total += self.hours[hour] / pulses_per_kwh
            if since is None or hour >= since:
                rows.append((hour, round(total, 6)))
        return rows

    def prune(self, now: int, pulses_per_kwh: float) -> None:
        """Fold hours older than KEEP_HOURS into the baseline."""
        cutoff = hour_of(now) - KEEP_HOURS * HOUR_S
        for hour in [hour for hour in self.hours if hour < cutoff]:
            self.baseline_kwh = (self.baseline_kwh or 0.0) + self.hours.pop(hour) / pulses_per_kwh

    def as_dict(self) -> dict[str, Any]:
        return {"baseline_kwh": self.baseline_kwh, "hours": {str(hour): pulses for hour, pulses in self.hours.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HourlyLedger:
        if not data:
            return cls()
        return cls(data.get("baseline_kwh"), {int(hour): int(pulses) for hour, pulses in data.get("hours", {}).items()})


class HourlyEnergyStatistic:
    """Keeps the ledger, and writes it to the recorder as an external statistic."""

    def __init__(self, hass: HomeAssistant, entry_id: str, address: str, name: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.hourly")
        self.statistic_id = f"{DOMAIN}:energy_{address.lower().replace(':', '_')}"
        self.name = f"{name} Energy (hourly)"
        self.ledger = HourlyLedger()

    @property
    def available(self) -> bool:
        return "recorder" in self._hass.config.components

    async def async_load(self) -> None:
        self.ledger = HourlyLedger.from_dict(await self._store.async_load())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _save(self) -> None:
        self._store.async_delay_save(self.ledger.as_dict, 10)

    async def async_seed(self, energy_entity_id: str | None) -> None:
        """On first use, copy the energy sensor's hourly history and carry on from it."""
        if self.ledger.baseline_kwh is not None or not self.available:
            return
        rows: list[dict[str, Any]] = []
        if energy_entity_id is not None:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period

            def _history() -> list[dict[str, Any]]:
                found = statistics_during_period(
                    self._hass,
                    dt_util.utc_from_timestamp(0),
                    None,
                    {energy_entity_id},
                    "hour",
                    {"energy": "kWh"},
                    {"sum", "state"},
                )
                return list(found.get(energy_entity_id, []))

            try:
                rows = await get_instance(self._hass).async_add_executor_job(_history)
            except Exception as err:
                _LOGGER.debug("Couldn't read %s's hourly statistics: %s", energy_entity_id, err)

        copied = [(int(row["start"]), float(row["sum"])) for row in rows if row.get("sum") is not None]
        self.ledger.baseline_kwh = copied[-1][1] if copied else 0.0
        if copied:
            _LOGGER.info("Copied %s hours of history into %s from %s", len(copied), self.statistic_id, energy_entity_id)
            self._write(copied)
        self._save()

    def add(self, timestamp: int, pulses: int) -> None:
        self.ledger.add(timestamp, pulses)

    def flush(self, pulses_per_kwh: float) -> None:
        """Write the hours changed since the last flush (and every hour after them)."""
        if self.ledger.dirty_from is None:
            return
        if self.ledger.baseline_kwh is None:
            self.ledger.baseline_kwh = 0.0
        rows = self.ledger.rows(pulses_per_kwh, since=self.ledger.dirty_from)
        self.ledger.dirty_from = None
        self.ledger.prune(int(dt_util.utcnow().timestamp()), pulses_per_kwh)
        self._save()
        if self.available:
            self._write(rows)

    def _write(self, rows: list[tuple[int, float]]) -> None:
        from homeassistant.components.recorder.models import StatisticMeanType
        from homeassistant.components.recorder.statistics import async_add_external_statistics
        from homeassistant.const import UnitOfEnergy

        async_add_external_statistics(
            self._hass,
            {
                "mean_type": StatisticMeanType.NONE,
                "has_sum": True,
                "name": self.name,
                "source": DOMAIN,
                "statistic_id": self.statistic_id,
                "unit_class": "energy",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            },
            [
                {"start": datetime.fromtimestamp(hour, dt_util.UTC), "sum": total, "state": total}
                for hour, total in rows
            ],
        )


def async_clear_hourly_statistic(hass: HomeAssistant, address: str) -> None:
    """Delete the hourly statistic, so re-adding the Powerpal starts it afresh.

    Otherwise rows written before would mix with the ones the new entry writes.
    """
    if "recorder" not in hass.config.components:
        return
    from homeassistant.components.recorder import get_instance

    get_instance(hass).async_clear_statistics([f"{DOMAIN}:energy_{address.lower().replace(':', '_')}"])
