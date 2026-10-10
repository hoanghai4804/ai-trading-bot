import pytest

from tradingbot.risk.sizing import check_entry_price, compute_stop, gap_pct, position_size


@pytest.mark.parametrize("ref,atr_value,mult,expected", [
    (100.0, 1.0, 2.0, 98.00),
    (100.0, 1.2345, 2.0, 97.53),    # 97.531 -> rounded down
    (100.0, 1.2399, 2.0, 97.52),    # 97.5202 -> down, never up to 97.53
    (50.0, 0.333, 2.0, 49.33),      # 49.334
])
def test_stop_is_ref_minus_k_atr_rounded_down_to_cent(ref, atr_value, mult, expected):
    assert compute_stop(ref, atr_value, mult) == expected


def test_gap_pct_is_signed():
    assert gap_pct(103, 100) == pytest.approx(0.03)
    assert gap_pct(97, 100) == pytest.approx(-0.03)


@pytest.mark.parametrize("ref,ok", [(103.0, True), (97.0, True), (103.01, False), (96.5, False)])
def test_gap_check_boundary_is_inclusive(ref, ok):
    check = check_entry_price(ref, last_close=100.0, stop_price=90.0, max_gap_pct=0.03)
    assert check.ok is ok
    if not ok:
        assert check.code == "gap_too_large" and check.detail


@pytest.mark.parametrize("stop", [100.0, 101.0, 0.0, -5.0])
def test_invalid_stop_is_rejected(stop):
    check = check_entry_price(100.0, 100.0, stop, 0.03)
    assert (check.ok, check.code) == (False, "invalid_stop")


def test_size_limited_by_position_cap():
    # risk: 1% * 100k / (100 - 96) = 250 shares; cap: 5% * 100k / 100 = 50 shares
    s = position_size(100_000, 100.0, 96.0, 100_000, 0.01, 0.05)
    assert (s.qty, s.limited_by, s.risk_qty, s.cap_qty) == (50, "position_cap", 250, 50)


def test_size_limited_by_risk():
    # risk: 1000 / (100 - 60) = 25 shares -> max loss at stop = 25 * 40 = 1000 = 1% equity
    s = position_size(100_000, 100.0, 60.0, 100_000, 0.01, 0.05)
    assert (s.qty, s.limited_by) == (25, "risk")
    assert s.qty * (100.0 - 60.0) <= 0.01 * 100_000


def test_size_limited_by_cash_and_never_negative():
    s = position_size(100_000, 100.0, 96.0, 1_050, 0.01, 0.05)
    assert (s.qty, s.limited_by) == (10, "cash")
    assert position_size(100_000, 100.0, 96.0, -500, 0.01, 0.05).qty == 0


def test_size_floor_ignores_float_noise():
    # 0.01 * 100000 / 4 may evaluate to 249.999...; must still be 250
    assert position_size(100_000, 100.0, 96.0, 1e9, 0.01, 0.9).risk_qty == 250


def test_size_rejects_bad_inputs():
    with pytest.raises(ValueError):
        position_size(100_000, 100.0, 100.0, 1e6, 0.01, 0.05)
    with pytest.raises(ValueError):
        position_size(0, 100.0, 90.0, 1e6, 0.01, 0.05)


def test_stop_accepts_numpy_floats():
    import numpy as np

    assert compute_stop(np.float64(100.0), np.float64(1.2345), 2.0) == 97.53
