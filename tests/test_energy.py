"""Counting records once, and on the right day."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.powerpal_direct.energy import CLOCK_RESET_THRESHOLD_S, EnergyAccumulator, EnergyState
from custom_components.powerpal_direct.protocol import Measurement

TZ = ZoneInfo("Australia/Melbourne")


def ts(*args: int) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


def test_counts_each_record_once() -> None:
    acc = EnergyAccumulator(EnergyState(), TZ)
    first = acc.add(Measurement(ts(2026, 10, 14, 12, 0), 5))
    assert first is not None and first.interval_s is None
    second = acc.add(Measurement(ts(2026, 10, 14, 12, 1), 7))
    assert second is not None and second.interval_s == 60
    # A replay of either, as history overlapping what was already counted.
    assert acc.add(Measurement(ts(2026, 10, 14, 12, 0), 5)) is None
    assert acc.add(Measurement(ts(2026, 10, 14, 12, 1), 7)) is None
    assert acc.state.total_pulses == 12
    assert acc.state.daily_pulses == 12
    assert acc.watermark == ts(2026, 10, 14, 12, 1)


def test_replayed_records_book_to_their_own_day() -> None:
    state = EnergyState(total_pulses=100, daily_pulses=40, daily_date="2026-10-14", watermark=ts(2026, 10, 14, 23, 50))
    acc = EnergyAccumulator(state, TZ)
    # Catch-up after midnight: the first records are still yesterday's.
    acc.add(Measurement(ts(2026, 10, 14, 23, 59), 3))
    assert (state.daily_date, state.daily_pulses) == ("2026-10-14", 43)
    acc.add(Measurement(ts(2026, 10, 15, 0, 0), 2))
    assert (state.daily_date, state.daily_pulses) == ("2026-10-15", 2)
    assert state.total_pulses == 105


def test_late_record_from_a_finished_day_only_adds_to_total() -> None:
    # The midnight timer already started today; a record from yesterday then turns up.
    state = EnergyState(total_pulses=10, daily_pulses=0, daily_date="2026-10-15", watermark=ts(2026, 10, 14, 23, 58))
    acc = EnergyAccumulator(state, TZ)
    acc.add(Measurement(ts(2026, 10, 14, 23, 59), 4))
    assert state.total_pulses == 14
    assert (state.daily_date, state.daily_pulses) == ("2026-10-15", 0)


def test_roll_day_only_moves_forward() -> None:
    state = EnergyState(daily_pulses=9, daily_date="2026-10-15")
    acc = EnergyAccumulator(state, TZ)
    assert not acc.roll_day(date(2026, 10, 14))
    assert not acc.roll_day(date(2026, 10, 15))
    assert state.daily_pulses == 9
    assert acc.roll_day(date(2026, 10, 16))
    assert (state.daily_date, state.daily_pulses) == ("2026-10-16", 0)


def test_clock_set_far_back_resumes_counting() -> None:
    watermark = ts(2026, 10, 14, 12, 0)
    state = EnergyState(total_pulses=10, daily_date="2026-10-14", watermark=watermark)
    acc = EnergyAccumulator(state, TZ)
    reset_to = watermark - CLOCK_RESET_THRESHOLD_S - 60
    result = acc.add(Measurement(reset_to, 3))
    assert result is not None and result.clock_reset
    assert state.total_pulses == 13
    assert acc.watermark == reset_to


def test_state_round_trips() -> None:
    state = EnergyState(total_pulses=1, daily_pulses=2, daily_date="2026-10-14", watermark=3)
    assert EnergyState.from_dict(state.as_dict()) == state
    assert EnergyState.from_dict(None) == EnergyState()


def test_a_saved_zero_watermark_means_none() -> None:
    assert EnergyState.from_dict({"total_pulses": 0, "watermark": 0}).watermark is None


def test_records_re_stamped_by_a_clock_nudge_are_not_counted_twice() -> None:
    # As seen on a real Powerpal: while released, something nudged its clock forward a
    # second, twice, and the replay included already-counted minutes re-stamped.
    base = ts(2026, 10, 16, 12, 33)
    acc = EnergyAccumulator(EnergyState(watermark=base, total_pulses=73), TZ)
    replayed = [
        (base + 1, 17),  # 12:33:00 again, re-stamped
        (base + 61, 16),
        (base + 121, 15),
        (base + 181, 14),
        (base + 182, 14),  # 12:36:01 again, re-stamped
    ]
    counted = [acc.add(Measurement(t, p)) for t, p in replayed]
    assert [c is not None for c in counted] == [False, True, True, True, False]
    assert acc.state.total_pulses == 73 + 16 + 15 + 14
    assert acc.watermark == base + 182
    # The next real minute, on the shifted grid, still counts, a minute after.
    assert acc.add(Measurement(base + 242, 13)) is not None


def test_watermark_moves_with_a_clock_correction_so_nothing_is_lost() -> None:
    # The Powerpal's clock was 120 s ahead; after it's set back, its next readings carry
    # timestamps 120 s earlier than before. Without moving the watermark they'd all look
    # already counted until the clock caught up again.
    base = ts(2026, 10, 16, 12, 0)
    acc = EnergyAccumulator(EnergyState(watermark=base, total_pulses=10), TZ)
    acc.shift_watermark(-120)
    assert acc.watermark == base - 120
    result = acc.add(Measurement(base - 60, 7))  # the next minute, on the corrected clock
    assert result is not None and result.interval_s == 60
    assert acc.state.total_pulses == 17


def test_shift_watermark_with_nothing_counted_yet() -> None:
    acc = EnergyAccumulator(EnergyState(), TZ)
    acc.shift_watermark(60)
    assert acc.watermark is None
