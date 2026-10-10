"""Which report a run produces, from config `schedule.runs` (New York weekday), and whether a
scheduled (cloud) run should run at all: `should-run` (RUN / SKIP / HALT, mode, exact command)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from tradingbot.config import Config

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def scheduled_report(cfg: Config, today: date) -> str | None:
    """'full' / 'brief' when `today` is a scheduled run day, else None."""
    day = WEEKDAYS[today.weekday()]
    return next((r.report for r in cfg.schedule.runs if r.weekday == day), None)


def report_mode(cfg: Config, today: date) -> str:
    """Report to write for a run on `today`; an unscheduled (manual) run gets a brief report."""
    return scheduled_report(cfg, today) or "brief"


# --- should-run: the decision the weekly prompt follows -----------------------------------------
# The prompt never decides by itself whether to trade: it runs `should-run` and executes the command
# printed on its `command:` line. EXECUTE needs TRADINGBOT_MODE=execute (exact, case-insensitive);
# anything else is a DRY-RUN with a warning that the report must show at the top.

MODE_ENV_VAR = "TRADINGBOT_MODE"
EXIT_RUN, EXIT_SKIP, EXIT_HALT = 0, 3, 4


@dataclass(frozen=True)
class RunDecision:
    today: date                 # New York date of the run (= runs/<today>/)
    action: str                 # RUN / SKIP / HALT
    dry_run: bool
    report: str                 # full / brief
    reason: str
    warnings: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        return "DRY-RUN" if self.dry_run else "EXECUTE"

    @property
    def exit_code(self) -> int:
        return {"RUN": EXIT_RUN, "SKIP": EXIT_SKIP, "HALT": EXIT_HALT}[self.action]

    @property
    def command(self) -> str | None:
        if self.action != "RUN":
            return None
        return "python -m tradingbot run" + ("" if self.dry_run else " --execute")

    def lines(self) -> list[str]:
        out = [f"SHOULD-RUN: {self.action} mode={self.mode} report={self.report} date={self.today.isoformat()}",
               f"reason: {self.reason}"]
        out.append(f"command: {self.command or 'none'}")
        out.extend(f"WARNING: {w}" for w in self.warnings)
        return out


def mode_from_env(value: str | None) -> tuple[bool, tuple[str, ...]]:
    """(dry_run, warnings) from the TRADINGBOT_MODE value. Only 'execute' trades."""
    if value is not None and value.strip().lower() == "execute":
        return False, ()
    if value is None or not value.strip():
        return True, (f"{MODE_ENV_VAR} is not set -> DRY-RUN (no order is sent)",)
    shown = value.strip()[:20].encode("ascii", "replace").decode()
    return True, (f"{MODE_ENV_VAR}={shown!r} is not 'execute' -> DRY-RUN (no order is sent)",)


def decide_run(cfg: Config, today: date, mode_env: str | None, halt: Mapping[str, Any] | None,
               existing_summary: Mapping[str, Any] | None, force: str | None = None) -> RunDecision:
    """RUN / SKIP / HALT for a run started on `today` (New York date).

    - HALT.lock present -> HALT (nothing runs; the caller alerts and writes an aborted summary).
    - A successful EXECUTE run already wrote today's summary -> SKIP (never overwrite real outputs).
    - Not a scheduled day -> SKIP, unless `force` ('brief' / 'full': the report to write) and the mode is
      DRY-RUN (manual test runs).
    """
    dry_run, warnings = mode_from_env(mode_env)
    scheduled = scheduled_report(cfg, today)
    report = scheduled or "brief"

    if force not in (None, "brief", "full"):
        raise ValueError(f"force must be 'brief' or 'full', got {force!r}")

    def decision(action: str, reason: str, rep: str = report) -> RunDecision:
        return RunDecision(today, action, dry_run, rep, reason, warnings)

    if halt is not None:
        return decision("HALT", f"state/HALT.lock present ({halt.get('reason', 'unknown')}); "
                                "a human must review and delete it", "brief")
    if existing_summary and existing_summary.get("mode") == "EXECUTE" and existing_summary.get("status") == "ok":
        return decision("SKIP", f"an EXECUTE run already completed today ({today.isoformat()})")
    if scheduled:
        return decision("RUN", f"{WEEKDAYS[today.weekday()]} is scheduled ({scheduled} report)")
    if force and dry_run:
        return decision("RUN", f"{WEEKDAYS[today.weekday()]} is not scheduled; forced manual DRY-RUN", force)
    if force:
        return decision("SKIP", f"{WEEKDAYS[today.weekday()]} is not scheduled; --force is refused in EXECUTE mode")
    return decision("SKIP", f"{WEEKDAYS[today.weekday()]} is not a scheduled run day")
