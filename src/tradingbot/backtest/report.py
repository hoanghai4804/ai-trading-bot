"""Build every backtest result and write reports/backtest/.

Periods (same data, fresh capital in each):
- full: backtest.start + warmup_months -> last session (indicators are warm at the start);
- oos:  first walk-forward test window -> last session (walk-forward is only defined there).

Files: report.md (Vietnamese, for the user), comparison.csv, yearly.csv,
walk_forward_params.csv, breaker_events.csv, blocked_reasons.csv, trades.csv,
trades_walk_forward.csv, sensitivity.csv, equity.csv, equity.svg.
Sensitivity runs only change a COPY of the risk config; config.yaml is never modified.
"""

from __future__ import annotations

import html
import logging
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.backtest.benchmarks import (RandomStrategy, buy_and_hold, calibrate_random, distribution,
                                            sma_timing)
from tradingbot.backtest.engine import BacktestResult, Trade, fixed_strategy, run_backtest
from tradingbot.backtest.metrics import Metrics, compute_metrics, yearly_returns
from tradingbot.backtest.walk_forward import (MapFn, WindowChoice, grid, make_windows, run_walk_forward,
                                              select_parameters)
from tradingbot.config import Config

METRIC_COLUMNS = ["total_return", "annual_return", "sharpe", "max_drawdown", "exposure", "trades", "win_rate",
                  "avg_holding_days", "costs"]
SENSITIVITY_STOPS = (2.0, 2.5, 3.0)
SENSITIVITY_CAPS = (0.05, 0.10)


@dataclass(frozen=True)
class Period:
    name: str
    start: date
    end: date


def _add_months(d: date, months: int) -> date:
    return (pd.Timestamp(d) + pd.DateOffset(months=months)).date()


def _quiet() -> None:
    """Warm-up weeks log 'not enough bars' for every symbol; irrelevant in a backtest."""
    logging.getLogger("tradingbot.strategies").setLevel(logging.ERROR)


# --- picklable jobs (run in worker processes) --------------------------------------------

def _random_job(job: tuple[Config, Mapping[str, pd.DataFrame], Period, int, float, float]) -> BacktestResult:
    cfg, bars, period, seed, p_entry, p_exit = job
    _quiet()
    return run_backtest(cfg, bars, period.start, period.end,
                        lambda session, window: RandomStrategy(seed, p_entry, p_exit), name=f"random {seed}")


def _sensitivity_job(job: tuple[Config, Mapping[str, pd.DataFrame], Period, float, float]) -> BacktestResult:
    cfg, bars, period, stop_mult, cap = job
    _quiet()
    risk = cfg.risk.model_copy(update={"atr_stop_multiple": stop_mult, "max_position_pct": cap})
    return run_backtest(cfg, bars, period.start, period.end, fixed_strategy(cfg), risk=risk,
                        name=f"stop {stop_mult}xATR, cap {cap:.0%}")


# --- building results ----------------------------------------------------------------------

@dataclass
class BacktestBundle:
    cfg: Config
    periods: dict[str, Period]
    fixed: dict[str, BacktestResult]
    fixed_stop: dict[str, BacktestResult]      # halt_mode="stop" variant
    buy_hold: dict[str, BacktestResult]
    sma200: dict[str, BacktestResult]
    random: dict[str, list[BacktestResult]]
    random_params: dict[str, tuple[float, float]]
    walk_forward: BacktestResult
    choices: list[WindowChoice]
    sensitivity: list[tuple[float, float, BacktestResult]]
    data_info: dict[str, Any]


def build(cfg: Config, bars: Mapping[str, pd.DataFrame], data_info: Mapping[str, Any] | None = None,
          map_fn: MapFn = map, seeds: int | None = None) -> BacktestBundle:
    _quiet()
    bench = cfg.benchmark_symbol
    data_end = max(f.index[-1] for f in bars.values()).date()
    windows = make_windows(cfg.backtest.start, data_end, cfg.backtest.train_years, cfg.backtest.test_months)
    periods = {
        "full": Period("full", _add_months(cfg.backtest.start, cfg.backtest.warmup_months), data_end),
        "oos": Period("oos", windows[0].test_start, data_end),
    }
    seeds = cfg.backtest.random_seeds if seeds is None else seeds
    fixed, fixed_stop, bh, sma, rnd, rnd_params = {}, {}, {}, {}, {}, {}
    for key, p in periods.items():
        fixed[key] = run_backtest(cfg, bars, p.start, p.end, fixed_strategy(cfg), name="MA20/50")
        fixed_stop[key] = run_backtest(cfg, bars, p.start, p.end, fixed_strategy(cfg), halt_mode="stop",
                                       name="MA20/50 (HALT = stop)")
        bh[key] = buy_and_hold(cfg, bars[bench], p.start, p.end, name=f"{bench} buy & hold")
        sma[key] = sma_timing(cfg, bars[bench], p.start, p.end, name=f"SMA200 {bench}")
        rnd_params[key] = calibrate_random(fixed[key], len(cfg.watchlist))
        rnd[key] = list(map_fn(_random_job, [(cfg, bars, p, s, *rnd_params[key]) for s in range(seeds)]))
    choices = select_parameters(cfg, bars, windows, map_fn)
    wf = run_walk_forward(cfg, bars, choices, name="walk-forward")
    full = periods["full"]
    sens_jobs = [(cfg, bars, full, m, c) for c in SENSITIVITY_CAPS for m in SENSITIVITY_STOPS]
    sens = [(j[3], j[4], r) for j, r in zip(sens_jobs, map_fn(_sensitivity_job, sens_jobs))]
    return BacktestBundle(cfg, periods, fixed, fixed_stop, bh, sma, rnd, rnd_params, wf, choices, sens,
                          dict(data_info or {}))


def metrics_of(r: BacktestResult) -> Metrics:
    return compute_metrics(r.equity, r.exposure, r.trades, r.costs_paid)


def exposure_matched(benchmark: BacktestResult, exposure: float) -> pd.Series:
    """Benchmark held at a constant `exposure`, rest in cash (0%), rebalanced daily."""
    r = benchmark.equity.pct_change().fillna(0.0)
    return benchmark.equity.iloc[0] * (1 + exposure * r).cumprod()


# --- tables ---------------------------------------------------------------------------------

def comparison_table(b: BacktestBundle) -> pd.DataFrame:
    rows = []
    for key, period in b.periods.items():
        named = [b.fixed[key], b.fixed_stop[key], b.buy_hold[key], b.sma200[key]]
        if key == "oos":
            named.insert(1, b.walk_forward)
        for r in named:
            rows.append({"period": key, "start": period.start, "end": period.end, "name": r.name,
                         **{c: getattr(metrics_of(r), c) for c in METRIC_COLUMNS}})
        ms = [metrics_of(r) for r in b.random[key]]
        for label, idx in (("median", 0), ("p5", 1), ("p95", 2)):
            rows.append({"period": key, "start": period.start, "end": period.end,
                         "name": f"random x{len(ms)} ({label})",
                         **{c: distribution([getattr(m, c) for m in ms])[idx] for c in METRIC_COLUMNS}})
    return pd.DataFrame(rows)


def yearly_table(b: BacktestBundle) -> pd.DataFrame:
    capital = b.cfg.backtest.initial_capital
    cols = {
        "MA20/50": yearly_returns(b.fixed["full"].equity.iloc[1:], capital),
        b.buy_hold["full"].name: yearly_returns(b.buy_hold["full"].equity.iloc[1:], capital),
        "walk-forward (oos)": yearly_returns(b.walk_forward.equity.iloc[1:], capital),
    }
    table = pd.DataFrame(cols)
    table["MA20/50 exposure"] = b.fixed["full"].exposure.iloc[1:].groupby(
        b.fixed["full"].exposure.index[1:].year).mean()
    table.index.name = "year"
    return table


def walk_forward_table(b: BacktestBundle) -> pd.DataFrame:
    rows = []
    for c in b.choices:
        w = c.window
        rows.append({"train_start": w.train_start, "train_end": w.train_end, "test_start": w.test_start,
                     "test_end": w.test_end, "fast": c.params[0], "slow": c.params[1],
                     **{f"sharpe_{f}_{s}": v for (f, s), v in c.scores.items()}})
    return pd.DataFrame(rows)


def events_table(b: BacktestBundle) -> pd.DataFrame:
    rows = []
    runs = [("MA20/50 full", b.fixed["full"]), ("MA20/50 oos", b.fixed["oos"]), ("walk-forward", b.walk_forward),
            ("MA20/50 full (HALT = stop)", b.fixed_stop["full"]),
            *((f"sensitivity: {r.name}", r) for _, _, r in b.sensitivity),
            *((f"random full seed {k}", r) for k, r in enumerate(b.random["full"]))]
    for label, r in runs:
        for e in r.events:
            rows.append({"run": label, "session": e.session, "kind": e.kind, "equity": round(e.equity, 2),
                         "peak": round(e.peak, 2), "drawdown": e.drawdown,
                         "previous_week_return": e.previous_week_return,
                         "positions_sold": " ".join(e.positions_sold), "detail": e.detail})
    return pd.DataFrame(rows, columns=["run", "session", "kind", "equity", "peak", "drawdown",
                                       "previous_week_return", "positions_sold", "detail"])


def blocked_table(b: BacktestBundle) -> pd.DataFrame:
    runs = {"MA20/50 full": b.fixed["full"], "walk-forward": b.walk_forward}
    counts = {k: r.blocked_counts() for k, r in runs.items()}
    rnd = Counter()
    for r in b.random["full"]:
        rnd.update(r.blocked_counts())
    counts[f"random full (mean of {len(b.random['full'])})"] = Counter(
        {k: v / max(len(b.random["full"]), 1) for k, v in rnd.items()})
    codes = sorted(set().union(*counts.values()))
    table = pd.DataFrame({k: [c.get(code, 0) for code in codes] for k, c in counts.items()}, index=codes)
    table.index.name = "code"
    return table


def trades_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": t.symbol, "entry_date": t.entry_date, "ref_price": round(t.ref_price, 4),
        "entry_price": round(t.entry_price, 4), "qty": t.qty, "stop": t.stop, "exit_date": t.exit_date,
        "exit_price": round(t.exit_price, 4) if t.exit_price is not None else None, "exit_reason": t.exit_reason,
        "pnl": round(t.pnl, 2), "return_pct": round(t.return_pct, 6), "holding_days": t.holding_days,
        "costs": round(t.costs, 2), "entry_reason": t.entry_reason,
    } for t in trades])


def sensitivity_table(b: BacktestBundle) -> pd.DataFrame:
    rows = []
    for stop_mult, cap, r in b.sensitivity:
        m = metrics_of(r)
        rows.append({"atr_stop_multiple": stop_mult, "max_position_pct": cap,
                     "is_config": stop_mult == b.cfg.risk.atr_stop_multiple and cap == b.cfg.risk.max_position_pct,
                     **{c: getattr(m, c) for c in METRIC_COLUMNS},
                     "halt_events": sum(1 for e in r.events if e.kind == "drawdown_halt")})
    return pd.DataFrame(rows)


def buy_hold_check(b: BacktestBundle, bars: Mapping[str, pd.DataFrame]) -> dict[str, float]:
    """Hand computation from the data: last adjusted close / first adjusted open - 1."""
    frame = bars[b.cfg.benchmark_symbol]
    p = b.periods["full"]
    window = frame.loc[pd.Timestamp(p.start):pd.Timestamp(p.end)]
    by_hand = float(window["close"].iloc[-1] / window["open"].iloc[0] - 1)
    simulated = metrics_of(b.buy_hold["full"]).total_return
    return {"by_hand": by_hand, "simulated": simulated, "difference": simulated - by_hand,
            "first_open": float(window["open"].iloc[0]), "last_close": float(window["close"].iloc[-1])}


# --- chart ------------------------------------------------------------------------------------

def equity_svg(series: list[tuple[str, pd.Series, str]], title: str, width: int = 960, height: int = 440) -> str:
    """Static line chart (normalized to 1.0 at the start), direct labels at the line ends + legend."""
    left, right, top, bottom = 56, 190, 44, 36
    pw, ph = width - left - right, height - top - bottom
    norm = [(html.escape(n), s / s.iloc[0], c) for n, s, c in series]
    title = html.escape(title)
    x0, x1 = norm[0][1].index[0], norm[0][1].index[-1]
    lo = min(float(s.min()) for _, s, _ in norm)
    hi = max(float(s.max()) for _, s, _ in norm)
    lo, hi = math.floor(lo * 10) / 10, math.ceil(hi * 10) / 10
    span = (x1 - x0).total_seconds()

    def x(ts: pd.Timestamp) -> float:
        return left + (ts - x0).total_seconds() / span * pw

    def y(v: float) -> float:
        return top + (hi - v) / (hi - lo) * ph

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
           f'height="{height}" font-family="system-ui, sans-serif" font-size="12">',
           '<style>.bg{fill:#fcfcfb}.t1{fill:#0b0b0b}.t2{fill:#52514e}.grid{stroke:#e4e3df}'
           '@media (prefers-color-scheme: dark){.bg{fill:#1a1a19}.t1{fill:#f0efec}.t2{fill:#c3c2b7}'
           '.grid{stroke:#3a3a37}}</style>',
           f'<rect class="bg" width="{width}" height="{height}"/>',
           f'<text class="t1" x="{left}" y="22" font-size="15" font-weight="600">{title}</text>']
    step = 0.5 if hi - lo > 2 else 0.25 if hi - lo > 0.8 else 0.1
    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        out.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="t2" x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.2f}</text>')
        v += step
    for year in range(x0.year + 1, x1.year + 1):
        ts = pd.Timestamp(year=year, month=1, day=1)
        out.append(f'<text class="t2" x="{x(ts):.1f}" y="{top + ph + 20}" text-anchor="middle">{year}</text>')
    label_y = []
    for name, s, color in norm:
        weekly = s.resample("W-FRI").last().dropna()
        pts = " ".join(f"{x(t):.1f},{y(val):.1f}" for t, val in weekly.items())
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}">'
                   f'<title>{name}: {s.iloc[-1]:.2f}x</title></polyline>')
        label_y.append([y(float(s.iloc[-1])), name, color, float(s.iloc[-1])])
    label_y.sort()
    for k in range(1, len(label_y)):            # keep end labels 16px apart
        label_y[k][0] = max(label_y[k][0], label_y[k - 1][0] + 16)
    for ly, name, color, last in label_y:
        out.append(f'<circle cx="{left + pw + 8}" cy="{ly - 4:.1f}" r="4" fill="{color}"/>')
        out.append(f'<text class="t1" x="{left + pw + 16}" y="{ly:.1f}">{name} {last:.2f}x</text>')
    out.append("</svg>")
    return "\n".join(out)


# --- markdown -------------------------------------------------------------------------------

def _pct(v: Any) -> str:
    return "–" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:+.1%}"


def _pct0(v: Any) -> str:
    return "–" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.1%}"


def _num(v: Any, fmt: str = ".2f") -> str:
    return "–" if v is None or (isinstance(v, float) and not np.isfinite(v)) else format(v, fmt)


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(r) + " |" for r in rows)]


def _comparison_rows(table: pd.DataFrame, period: str) -> list[list[str]]:
    rows = []
    sub = table[table["period"] == period]
    rnd = sub[sub["name"].str.startswith("random")]
    for _, r in sub[~sub["name"].str.startswith("random")].iterrows():
        rows.append([r["name"], _pct(r.total_return), _pct(r.annual_return), _num(r.sharpe), _pct0(r.max_drawdown),
                     _pct0(r.exposure), _num(r.trades, ".0f"), _pct0(r.win_rate), _num(r.avg_holding_days, ".0f"),
                     _num(r.costs, ",.0f")])
    if not rnd.empty:
        med, p5, p95 = (rnd.iloc[k] for k in range(3))

        def cell(col: str, f) -> str:
            return f"{f(med[col])} [{f(p5[col])}; {f(p95[col])}]"

        n = med["name"].split(" (")[0]
        rows.append([f"{n}: trung vị [p5; p95]", cell("total_return", _pct), cell("annual_return", _pct),
                     cell("sharpe", _num), cell("max_drawdown", _pct0), cell("exposure", _pct0),
                     cell("trades", lambda v: _num(v, ".0f")), cell("win_rate", _pct0),
                     cell("avg_holding_days", lambda v: _num(v, ".0f")), cell("costs", lambda v: _num(v, ",.0f"))])
    return rows


COMPARISON_HEADER = ["Chiến lược", "Tổng LN", "LN/năm", "Sharpe", "Max DD", "Exposure TB", "Số lệnh",
                     "Tỷ lệ thắng", "Giữ TB (ngày)", "Chi phí ($)"]


def write_reports(b: BacktestBundle, bars: Mapping[str, pd.DataFrame], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = b.cfg
    comp = comparison_table(b)
    yearly = yearly_table(b)
    wf = walk_forward_table(b)
    events = events_table(b)
    blocked = blocked_table(b)
    sens = sensitivity_table(b)
    check = buy_hold_check(b, bars)
    fixed = b.fixed["full"]
    m_fixed = metrics_of(fixed)
    bench_name = b.buy_hold["full"].name
    matched = exposure_matched(b.buy_hold["full"], m_fixed.exposure)
    equity = pd.DataFrame({"MA20/50": fixed.equity, bench_name: b.buy_hold["full"].equity,
                           f"{cfg.benchmark_symbol} x exposure {m_fixed.exposure:.0%}": matched,
                           "MA20/50 exposure": fixed.exposure})

    files = {
        "comparison.csv": comp, "yearly.csv": yearly, "walk_forward_params.csv": wf,
        "breaker_events.csv": events, "blocked_reasons.csv": blocked, "trades.csv": trades_frame(fixed.trades),
        "trades_walk_forward.csv": trades_frame(b.walk_forward.trades), "sensitivity.csv": sens,
        "equity.csv": equity,
    }
    written = []
    for name, frame in files.items():
        path = out_dir / name
        frame.to_csv(path, index=name in ("yearly.csv", "blocked_reasons.csv", "equity.csv"),
                     float_format="%.6g", lineterminator="\n")
        written.append(path)
    svg = equity_svg([("MA20/50", fixed.equity, "#2a78d6"), (bench_name, b.buy_hold["full"].equity, "#eb6834"),
                      (f"{cfg.benchmark_symbol} x {m_fixed.exposure:.0%}", matched, "#1baf7a")],
                     f"Equity (chuẩn hóa = 1.0) {b.periods['full'].start} → {b.periods['full'].end}")
    (out_dir / "equity.svg").write_text(svg + "\n", encoding="utf-8", newline="\n")
    written.append(out_dir / "equity.svg")
    md = _markdown(b, comp, yearly, wf, events, blocked, sens, check, m_fixed, matched)
    (out_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")
    written.append(out_dir / "report.md")
    return written


def _markdown(b: BacktestBundle, comp: pd.DataFrame, yearly: pd.DataFrame, wf: pd.DataFrame,
              events: pd.DataFrame, blocked: pd.DataFrame, sens: pd.DataFrame, check: dict[str, float],
              m_fixed: Metrics, matched: pd.Series) -> str:
    cfg, full, oos = b.cfg, b.periods["full"], b.periods["oos"]
    bench = cfg.benchmark_symbol
    info = b.data_info
    capital = cfg.backtest.initial_capital
    matched_ret = float(matched.iloc[-1] / matched.iloc[0] - 1)
    L: list[str] = [
        "# Báo cáo backtest — MA crossover hằng tuần",
        "",
        f"Sinh bởi `python -m tradingbot backtest`. Vốn ban đầu {capital:,.0f} $ ở mỗi giai đoạn. "
        f"Dữ liệu ngày {info.get('first_bar', '?')} → {info.get('last_bar', '?')} (feed `{info.get('feed', '?')}`, "
        "giá điều chỉnh chia tách + cổ tức). Chiến lược, risk manager, hàm stop/sizing/lệch giá là CÙNG code với "
        "lần chạy thật.",
        "",
        "## 1. Bảng so sánh",
        "",
        f"### Giai đoạn đầy đủ: {full.start} → {full.end}",
        "",
        *_md_table(COMPARISON_HEADER, _comparison_rows(comp, "full")),
        "",
        f"### Giai đoạn out-of-sample của walk-forward: {oos.start} → {oos.end}",
        "",
        *_md_table(COMPARISON_HEADER, _comparison_rows(comp, "oos")),
        "",
        "- Sharpe: lợi suất ngày, lãi suất phi rủi ro = 0, nhân √252. LN/năm theo 252 phiên/năm.",
        "- Exposure = giá trị cổ phiếu đang giữ / equity, trung bình theo ngày. Số lệnh = số vòng mua-bán đã đóng.",
        "- `HALT = stop`: biến thể drawdown breaker dừng hẳn (giữ tiền mặt đến hết); dòng chính tiếp tục tuần sau "
        "và đặt lại đỉnh tại ngày HALT.",
        f"- Ngẫu nhiên: cùng engine + risk manager + stop + breaker; mỗi tuần mỗi mã chưa giữ được đề xuất mua với "
        f"xác suất {b.random_params['full'][0]:.3f}, mỗi vị thế được đề xuất thoát với xác suất "
        f"{b.random_params['full'][1]:.3f} (hiệu chỉnh theo tần suất tín hiệu của MA20/50 giai đoạn đầy đủ).",
        f"- SMA200 {bench}: mỗi thứ Hai, close > SMA200 → 100% {bench}, ngược lại 100% tiền mặt; không stop.",
        "",
        f"**So với {bench} cùng mức exposure** ({m_fixed.exposure:.1%} {bench} + phần còn lại tiền mặt lãi 0%, "
        f"cân bằng hằng ngày): tổng lợi nhuận {matched_ret:+.1%}, so với MA20/50 {m_fixed.total_return:+.1%}.",
        "",
        f"**Kiểm tra mua-và-giữ {bench}**: tính tay close cuối {check['last_close']:.2f} / open đầu "
        f"{check['first_open']:.2f} − 1 = {check['by_hand']:+.3%}; mô phỏng = {check['simulated']:+.3%} "
        f"(chênh {check['difference']:+.3%}, do trượt giá lúc mua).",
        "",
        "## 2. Theo từng năm",
        "",
        *_md_table(["Năm", "MA20/50", b.buy_hold["full"].name, "Walk-forward", "Exposure MA20/50"],
                   [[str(y), _pct(r["MA20/50"]), _pct(r[b.buy_hold["full"].name]), _pct(r["walk-forward (oos)"]),
                     _pct0(r["MA20/50 exposure"])] for y, r in yearly.iterrows()]),
        "",
        "Năm đầu/cuối là năm không trọn. Walk-forward bắt đầu từ giai đoạn out-of-sample.",
        "",
        "![equity](equity.svg)",
        "",
        "## 3. Tham số walk-forward theo cửa sổ",
        "",
        f"Train {cfg.backtest.train_years} năm / test {cfg.backtest.test_months} tháng, chọn cặp có Sharpe train cao "
        f"nhất trong lưới {', '.join(f'{f}/{s}' for f, s in grid(cfg))} (hòa → thứ tự lưới). Train chỉ nhận dữ liệu "
        "đến ngày trước cửa sổ test. Cửa sổ train đầu tiên mất khoảng nửa năm khởi động chỉ báo (dữ liệu bắt đầu 2016).",
        "",
        *_md_table(["Test", "Train", "Chọn", "Sharpe train của cặp được chọn"],
                   [[f"{r.test_start} → {r.test_end}", f"{r.train_start} → {r.train_end}", f"{r.fast}/{r.slow}",
                     _num(r[f"sharpe_{r.fast}_{r.slow}"])] for _, r in wf.iterrows()]),
        "",
        "Sharpe train của mọi cặp: `walk_forward_params.csv`.",
        "",
        "## 4. Sự kiện circuit breaker",
        "",
    ]
    if events.empty:
        L.append("Không có sự kiện nào.")
    else:
        L += _md_table(["Lần chạy", "Ngày", "Loại", "Equity", "Đỉnh", "Drawdown", "LN tuần trước", "Đã bán"],
                       [[r.run, str(r.session), r.kind, f"{r.equity:,.0f}", f"{r.peak:,.0f}", _pct0(r.drawdown),
                         _pct(r.previous_week_return), r.positions_sold or "–"] for r in events.itertuples()])
    L += [
        "",
        "## 5. Lệnh mua bị chặn theo lý do",
        "",
        *_md_table(["Lý do", *blocked.columns],
                   [[code, *(_num(v, ".1f") if isinstance(v, float) and v % 1 else str(int(v)) for v in row)]
                    for code, row in blocked.iterrows()]),
        "",
        "## 6. Danh sách lệnh",
        "",
        f"`trades.csv` (MA20/50 giai đoạn đầy đủ, {len(b.fixed['full'].trades)} lệnh) và "
        f"`trades_walk_forward.csv` ({len(b.walk_forward.trades)} lệnh): ngày vào/ra, giá tham chiếu, giá khớp, "
        "số lượng, stop, lý do thoát (`signal`, `stop`, `stop_gap`, `drawdown_halt`, `open` = còn giữ cuối kỳ, "
        "định giá theo close cuối), lời/lỗ sau chi phí.",
        "",
        "## 7. Equity",
        "",
        f"`equity.svg` / `equity.csv`: MA20/50 so với {bench} mua-và-giữ và {bench} × {m_fixed.exposure:.0%} "
        "(cùng mức exposure trung bình).",
        "",
        "## 8. Độ nhạy (CHỈ để báo cáo, config không đổi)",
        "",
        f"MA20/50, giai đoạn đầy đủ. Config hiện tại: stop {cfg.risk.atr_stop_multiple:g}×ATR, "
        f"giới hạn vị thế {cfg.risk.max_position_pct:.0%}.",
        "",
        *_md_table(["Stop", "Giới hạn vị thế", "Tổng LN", "LN/năm", "Sharpe", "Max DD", "Exposure TB", "Số lệnh",
                    "Tỷ lệ thắng", "Số HALT"],
                   [[f"{r.atr_stop_multiple:g}×ATR" + (" (config)" if r.is_config else ""),
                     f"{r.max_position_pct:.0%}", _pct(r.total_return), _pct(r.annual_return), _num(r.sharpe),
                     _pct0(r.max_drawdown), _pct0(r.exposure), str(int(r.trades)), _pct0(r.win_rate),
                     str(int(r.halt_events))] for r in sens.itertuples()]),
        "",
        "## 9. Giới hạn của backtest",
        "",
        "- **Survivorship bias**: watchlist được chọn hôm nay từ những công ty thành công (NVDA, META, AAPL, MSFT, "
        "AMZN, GOOGL...). Năm 2016 không ai biết trước danh sách này, nên kết quả bị thổi phồng, nhất là với "
        "chiến lược chỉ mua (long-only) và với benchmark ngẫu nhiên trên cùng danh sách.",
        f"- **Dữ liệu**: Alpaca daily bars feed `{info.get('feed', '?')}` (SIP = toàn thị trường; IEX chỉ có từ "
        "2020-07 và là một phần nhỏ khối lượng). Chỉ có dữ liệu từ "
        f"{info.get('first_bar', '?')}, nên chỉ có ~{(full.end - full.start).days / 365.25:.1f} năm so sánh "
        "(không có 2008, 2000) và cửa sổ train đầu tiên ngắn hơn 3 năm thực tế.",
        "- **Giá điều chỉnh** (adjustment=all): cổ tức được cộng vào giá, nên lợi nhuận là tổng lợi nhuận; số cổ "
        "phiếu và phí theo cổ phiếu (TAF) tính trên giá điều chỉnh, khác chút với thực tế. Lần chạy thật dùng "
        "giá giao dịch thô và close thô cho kiểm tra lệch giá; backtest dùng open/close điều chỉnh.",
        "- **Giá vào lệnh**: lần chạy thật khớp lúc ~11:07 New York (giá giao dịch mới nhất); backtest dùng giá open "
        "thứ Hai + trượt giá 5 bps. Lệnh bán theo tín hiệu cũng khớp ở open.",
        "- **Stop**: khớp nếu low ≤ stop (ở giá stop) hoặc open ≤ stop (ở giá open), tính cả ngày vào lệnh. Không "
        "mô phỏng thứ tự giá trong ngày, thanh khoản, hay việc Alpaca hủy lệnh GTC sau 90 ngày (lần chạy thật đặt "
        "lại stop gốc mỗi tuần).",
        "- **Circuit breakers** chỉ xét lúc chạy thứ Hai (như thật). Lịch sử equity cho breaker = giá trị đóng cửa "
        "hằng ngày trong 1 năm gần nhất (như portfolio history 1A của Alpaca). Sau HALT, dòng chính giả định người "
        "dùng đã xem xét và xóa HALT.lock để chạy tiếp tuần sau với đỉnh mới; lần chạy thật cần người can thiệp.",
        "- **Giới hạn 3 vị thế/tuần**: chỉ chạy thứ Hai nên số lệnh mua trước đó trong tuần luôn là 0.",
        "- **Không mô phỏng**: lãi tiền mặt (0%), cổ tức nhận bằng tiền (đã nằm trong giá điều chỉnh), lỗi API, "
        "lệnh bị từ chối, nghỉ giao dịch đột xuất, thuế. Tương quan, ATR, MA tính trên cửa sổ "
        f"{cfg.data.history_days} ngày lịch như lần chạy thật.",
        "- **Mua-và-giữ / SMA200**: cổ phiếu lẻ (fractional), toàn bộ vốn, không cash buffer.",
        "- **Ngẫu nhiên**: cùng watchlist (cùng survivorship bias), nên nó đo giá trị của *thời điểm* vào/ra, "
        "không đo giá trị của việc chọn cổ phiếu.",
        "- Walk-forward chỉ có lưới 6 cặp; số cửa sổ ít nên kết quả chọn tham số có nhiễu lớn.",
        "",
    ]
    if b.fixed["full"].skipped_mondays:
        L.insert(-1, f"- Thứ Hai nghỉ lễ (không giao dịch tuần đó, cửa sổ tín hiệu tuần sau mở rộng): "
                     f"{len(b.fixed['full'].skipped_mondays)} lần trong giai đoạn đầy đủ.")
    return "\n".join(L)
