import numpy as np
import pandas as pd
import pytest

from tradingbot.indicators.technical import atr, daily_returns, return_correlation, sma, true_range


def _bars(rows):
    idx = pd.date_range("2026-01-05", periods=len(rows), freq="B")
    return pd.DataFrame(rows, columns=["high", "low", "close"], index=idx)


# Hand-computed example (period 3). The last bar gaps up from 10 to 19-20.
HAND_BARS = _bars([(10, 8, 9), (11, 9, 10), (12, 9, 11), (15, 11, 14), (14, 10, 10), (20, 19, 19.5)])


def test_sma_known_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_true_range_uses_previous_close_on_gaps():
    assert true_range(HAND_BARS).tolist() == [2, 2, 3, 4, 4, 10]  # last: |19 - 10| = 9 < |20 - 10| = 10


def test_atr_matches_wilder_by_hand():
    out = atr(HAND_BARS, period=3)
    assert out.iloc[:2].isna().all()
    expected = [7 / 3, 26 / 9, 88 / 27, 446 / 81]
    assert out.iloc[2:].to_numpy() == pytest.approx(expected)


def test_atr_short_history_is_all_nan():
    assert atr(HAND_BARS.iloc[:2], period=3).isna().all()


def test_daily_returns():
    r = daily_returns(pd.Series([100.0, 110, 99]))
    assert np.isnan(r.iloc[0]) and r.iloc[1:].tolist() == pytest.approx([0.10, -0.10])


def test_return_correlation_extremes_and_insufficient_data():
    rng = np.random.default_rng(0)
    base = 100 * np.cumprod(1 + rng.normal(0, 0.01, 80))
    base_returns = pd.Series(base).pct_change().to_numpy()
    mirror = [100.0]
    for r in base_returns[1:]:
        mirror.append(mirror[-1] * (1 - r))  # exactly opposite daily return
    closes = pd.DataFrame({"A": base, "B": base * 3, "C": mirror})
    closes.loc[:70, "D"] = base[:71]  # D stops early -> not enough overlap
    corr = return_correlation(closes, lookback=60)
    assert corr.loc["A", "B"] == pytest.approx(1.0)
    assert corr.loc["A", "C"] == pytest.approx(-1.0)
    assert np.isnan(corr.loc["A", "D"])


@pytest.mark.parametrize("func", [lambda b: sma(b["close"], 3), lambda b: atr(b, 3)])
def test_no_lookahead_appending_future_rows_never_changes_past_values(func):
    rng = np.random.default_rng(1)
    close = 100 + rng.normal(0, 1, 60).cumsum()
    full = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close},
                        index=pd.date_range("2026-01-05", periods=60, freq="B"))
    past = full.iloc[:40]
    pd.testing.assert_series_equal(func(past), func(full).iloc[:40], check_names=False)
