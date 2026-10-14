"""Turning Powerpal records into energy totals, without counting any minute twice.

Pure Python, no Home Assistant, so it can be tested on its own.

Every record the Powerpal sends, live or replayed from its history, covers one minute
and carries its own timestamp. A persisted watermark (the newest timestamp already
counted) is what makes replaying history safe: anything at or below it has been
counted, so catch-up can simply ask for everything after it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, tzinfo
from typing import Any

from .protocol import Measurement

# A record this far behind the watermark can't be a resend of one already counted: the
# Powerpal's clock has been set back (e.g. reset by a battery change). Counting resumes
# from the new clock, rather than ignoring every record until it catches up again.
CLOCK_RESET_THRESHOLD_S = 24 * 3600


@dataclass(slots=True)
class EnergyState:
    """What has to survive a restart."""

    total_pulses: int = 0
    daily_pulses: int = 0
    # The local date daily_pulses belongs to, ISO format.
    daily_date: str | None = None
    # Timestamp of the newest record already counted.
    watermark: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EnergyState:
        if not data:
            return cls()
        return cls(
            total_pulses=int(data.get("total_pulses", 0)),
            daily_pulses=int(data.get("daily_pulses", 0)),
            daily_date=data.get("daily_date"),
            watermark=data.get("watermark"),
        )


@dataclass(frozen=True, slots=True)
class CountedRecord:
    """A record that was new, and so has been added to the totals."""

    measurement: Measurement
    # Seconds since the previously counted record, or None for the first one ever.
    interval_s: int | None
    clock_reset: bool = False


class EnergyAccumulator:
    """Counts each record once, and books it to the day it was measured on."""

    def __init__(self, state: EnergyState, tz: tzinfo) -> None:
        self.state = state
        self._tz = tz

    @property
    def watermark(self) -> int | None:
        return self.state.watermark

    def set_watermark(self, timestamp: int) -> None:
        """Start counting after `timestamp`, e.g. on first setup, to skip old history."""
        self.state.watermark = timestamp

    def local_date(self, timestamp: int) -> date:
        return datetime.fromtimestamp(timestamp, self._tz).date()

    def add(self, measurement: Measurement) -> CountedRecord | None:
        """Count a record. Returns None if it had already been counted."""
        state = self.state
        clock_reset = False
        interval_s: int | None = None
        if state.watermark is not None:
            if measurement.timestamp <= state.watermark:
                if state.watermark - measurement.timestamp < CLOCK_RESET_THRESHOLD_S:
                    return None
                clock_reset = True
            else:
                interval_s = measurement.timestamp - state.watermark

        # The record's own timestamp decides the day, not the time it arrived: history
        # replayed after midnight belongs to yesterday, and yesterday's total is gone.
        record_date = self.local_date(measurement.timestamp)
        self.roll_day(record_date)
        if state.daily_date == record_date.isoformat():
            state.daily_pulses += measurement.pulses

        state.total_pulses += measurement.pulses
        state.watermark = measurement.timestamp
        return CountedRecord(measurement=measurement, interval_s=interval_s, clock_reset=clock_reset)

    def roll_day(self, today: date) -> bool:
        """Start a new daily total if `today` is later than the current one's day."""
        state = self.state
        if state.daily_date is not None and date.fromisoformat(state.daily_date) >= today:
            return False
        state.daily_date = today.isoformat()
        state.daily_pulses = 0
        return True
