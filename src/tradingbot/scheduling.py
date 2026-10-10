"""Which report a run produces, from config `schedule.runs` (New York weekday)."""

from __future__ import annotations

from datetime import date

from tradingbot.config import Config

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def scheduled_report(cfg: Config, today: date) -> str | None:
    """'full' / 'brief' when `today` is a scheduled run day, else None."""
    day = WEEKDAYS[today.weekday()]
    return next((r.report for r in cfg.schedule.runs if r.weekday == day), None)


def report_mode(cfg: Config, today: date) -> str:
    """Report to write for a run on `today`; an unscheduled (manual) run gets a brief report."""
    return scheduled_report(cfg, today) or "brief"
