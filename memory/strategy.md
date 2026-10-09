# Strategy

## Active
- `ma_crossover`: MA20/MA50 on daily bars, long-only. Parameters in `config/config.yaml`.

## Ideas for later phases (not implemented)
- Market regime detection with an HMM — use only the forward algorithm (filtering), never smoothing/Viterbi over the full sample (look-ahead).
- GARCH volatility forecast for position sizing and stop distance (replace ATR).
- ADF test to filter trending vs mean-reverting symbols.
- Mean-reversion signal (e.g. z-score vs moving average) for non-trending symbols.
- Pairs trading on cointegrated pairs (needs short side → would require revisiting long-only rule).
