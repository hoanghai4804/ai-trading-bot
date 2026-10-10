"""Plain-text run summary. The FIRST line always states the mode (DRY-RUN or EXECUTE)."""

from __future__ import annotations

from tradingbot.pipeline import RunResult

MODE_LINES = {
    "DRY-RUN": "MODE: DRY-RUN - simulation, no order was sent",
    "EXECUTE": "MODE: EXECUTE - orders were sent to the Alpaca PAPER account",
}


def mode_line(dry_run: bool) -> str:
    return MODE_LINES["DRY-RUN" if dry_run else "EXECUTE"]


def format_run(result: RunResult) -> list[str]:
    lines = [mode_line(result.dry_run)]
    lines.append(f"run today={result.today} as_of={result.as_of} market_open={result.market_open} "
                 f"signal_window={result.signal_window} sessions")
    lines.append(f"equity={result.equity:,.2f} cash={result.cash:,.2f} positions="
                 + (",".join(f"{s}:{q:g}" for s, q in sorted(result.positions.items())) or "-")
                 + f" new_positions_this_week={result.new_positions_this_week}")
    b = result.decision.breakers
    week = "unknown" if b.previous_week_return is None else f"{b.previous_week_return:+.2%}"
    lines.append(f"breakers: halt={b.halt} block_new={b.block_new_entries} prev_week={week} "
                 f"drawdown={b.drawdown:.2%}")
    for f in result.stop_fills:
        lines.append(f"stop filled since last week: {f.symbol} {f.qty:g} @ {f.price} on {f.day}")
    lines.append(f"signals={len(result.signals)}: "
                 + (", ".join(f"{s.action} {s.symbol}" for s in result.signals) or "-"))
    for o in result.decision.approved:
        extra = f" ref={o.ref_price:.2f} stop={o.stop_price:.2f} ({o.limited_by})" if o.side == "buy" else ""
        lines.append(f"approved {o.side:4} {o.symbol:6} qty={o.qty:g}{extra} | {o.reason}")
    for bl in result.decision.blocked:
        lines.append(f"blocked  {bl.side:4} {bl.symbol:6} {bl.code}: {bl.detail}")
    for r in result.execution.results:
        stop = f" stop={r.stop_price:.2f}" if r.stop_price else ""
        qty = f" qty={r.qty:g}" if r.qty is not None else ""
        lines.append(f"exec {r.status:9} {r.side:4} {r.symbol:6}{qty}{stop} id={r.client_order_id or '-'} {r.detail}")
    lines.append(f"orders_sent={result.execution.orders_sent}")
    lines.extend(f"WARNING {w}" for w in [*result.warnings, *result.execution.alerts])
    return lines
