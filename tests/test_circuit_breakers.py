from datetime import date

import pandas as pd
import pytest

from tradingbot.config import CircuitBreakerConfig
from tradingbot.risk.circuit_breakers import (
    equity_series,
    evaluate_circuit_breakers,
    previous_week_return,
    trip_halt,
    week_start,
)
from tradingbot.safety import read_halt

CFG = CircuitBreakerConfig(weekly_loss_pct=0.05, max_drawdown_pct=0.10)
MONDAY = date(2026, 10, 12)  # run day; previous week = Oct 5-11, the week before ends Sun Oct 4


def _history(values: dict[str, float]) -> pd.Series:
    return equity_series({date.fromisoformat(d): v for d, v in values.items()})


def test_week_start_is_monday():
    assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # Sunday -> its Monday
    assert week_start(MONDAY) == MONDAY


def test_previous_week_return_uses_last_value_of_each_calendar_week():
    h = _history({"2026-10-01": 99_000, "2026-10-02": 100_000,   # end of week before: Fri Oct 2
                  "2026-10-08": 97_000, "2026-10-09": 94_000,    # end of previous week: Fri Oct 9
                  "2026-10-12": 50_000})                          # today's value is ignored
    assert previous_week_return(h, MONDAY) == pytest.approx(-0.06)


def test_previous_week_return_works_mid_week_and_with_holiday_friday():
    h = _history({"2026-10-02": 100_000, "2026-10-08": 103_000})  # Fri Oct 9 missing (holiday)
    assert previous_week_return(h, date(2026, 10, 14)) == pytest.approx(0.03)


@pytest.mark.parametrize("values", [
    {},                                              # no history at all
    {"2026-10-06": 100_000, "2026-10-09": 90_000},   # funded during the previous week
    {"2026-10-02": 100_000},                         # no value inside the previous week
])
def test_previous_week_return_unknown(values):
    assert previous_week_return(_history(values), MONDAY) is None


@pytest.mark.parametrize("friday,tripped", [(95_000, False), (94_990, True)])
def test_weekly_loss_breaker_boundary(friday, tripped):
    h = _history({"2026-10-02": 100_000, "2026-10-09": friday})
    s = evaluate_circuit_breakers(friday, MONDAY, CFG, h)
    assert s.block_new_entries is tripped
    assert s.halt is False
    assert bool(s.reasons) is tripped


def test_unknown_weekly_return_does_not_block():
    s = evaluate_circuit_breakers(100_000, MONDAY, CFG, _history({}))
    assert s.previous_week_return is None and not s.block_new_entries


@pytest.mark.parametrize("current,halt", [(90_000, True), (90_001, False), (85_000, True)])
def test_drawdown_breaker_boundary(current, halt):
    h = _history({"2026-09-01": 100_000, "2026-10-09": 92_000})
    s = evaluate_circuit_breakers(current, MONDAY, CFG, h)
    assert s.halt is halt
    assert s.peak_equity == 100_000
    if halt:
        assert s.block_new_entries and "drawdown" in s.reasons[0]


def test_peak_includes_local_log_and_current_equity():
    broker = _history({"2026-09-01": 100_000})
    local = _history({"2026-09-15": 110_000})  # higher peak only in our own log
    s = evaluate_circuit_breakers(99_000, MONDAY, CFG, broker, [local])
    assert s.peak_equity == 110_000 and s.halt  # 1 - 99/110 = 10%
    s = evaluate_circuit_breakers(120_000, MONDAY, CFG, broker, [local])
    assert s.peak_equity == 120_000 and s.drawdown == 0


def test_trip_halt_writes_lock(tmp_path):
    h = _history({"2026-09-01": 100_000})
    s = evaluate_circuit_breakers(89_000, MONDAY, CFG, h)
    trip_halt(s, tmp_path)
    lock = read_halt(tmp_path)
    assert "drawdown 11.00%" in lock["reason"]
    assert lock["details"]["peak_equity"] == 100_000


def test_trip_halt_refuses_when_not_tripped(tmp_path):
    s = evaluate_circuit_breakers(100_000, MONDAY, CFG, _history({}))
    with pytest.raises(ValueError):
        trip_halt(s, tmp_path)
    assert read_halt(tmp_path) is None
