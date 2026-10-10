"""Keep memory/journal.md short: detailed entries for the last N calendar weeks only.

journal.md layout:
    # Journal
    <intro>
    ## Older weeks
    - 2026-09-07: <one-line summary>
    ## 2026-10-12
    <entry>                         (newest first)

An entry is a `## YYYY-MM-DD` section. Entries dated before the Monday of the week
(N - 1) weeks before today's are moved, full text, to journal_archive.md (not read by
default, oldest first, never duplicated) and replaced by one line in "Older weeks":
the entry's `Summary:` / `Tóm tắt:` line, else its first non-empty line. Idempotent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\b.*$")
OLDER_HEADING = "## Older weeks"
SUMMARY_RE = re.compile(r"^\s*[-*]?\s*(?:\*\*)?(?:Summary|Tóm tắt)(?:\*\*)?\s*:\s*(.+)$", re.IGNORECASE)
JOURNAL_INTRO = ("Detailed entries for the last {n} weeks, newest first. Older weeks: one line each below; "
                 "full text in journal_archive.md (do not read by default).")


@dataclass(frozen=True)
class Entry:
    day: date
    text: str          # heading line + body, no trailing blank lines

    def summary(self) -> str:
        body = self.text.splitlines()[1:]
        for line in body:
            m = SUMMARY_RE.match(line)
            if m:
                return _one_line(m.group(1))
        first = next((ln.strip(" -*#") for ln in body if ln.strip()), "(empty entry)")
        return _one_line(first)


def _one_line(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_journal(text: str) -> tuple[list[str], list[Entry]]:
    """(older-week summary lines, entries). Text outside those sections is regenerated."""
    older: list[str] = []
    entries: list[Entry] = []
    section: str | None = None
    current: list[str] = []

    def flush() -> None:
        if section == "entry" and current:
            entries.append(Entry(date.fromisoformat(ENTRY_RE.match(current[0]).group(1)),
                                 "\n".join(current).rstrip()))

    for line in text.splitlines():
        if line.strip() == OLDER_HEADING:
            flush()
            section, current = "older", []
        elif ENTRY_RE.match(line):
            flush()
            section, current = "entry", [line]
        elif line.startswith("## ") or line.startswith("# "):
            flush()
            section, current = None, []
        elif section == "older":
            if line.strip().startswith("- "):
                older.append(line.strip())
        elif section == "entry":
            current.append(line)
    flush()
    return older, entries


def cutoff_date(today: date, keep_weeks: int) -> date:
    """Monday of the oldest calendar week kept in detail."""
    return today - timedelta(days=today.weekday()) - timedelta(weeks=keep_weeks - 1)


def render_journal(older: list[str], entries: list[Entry], keep_weeks: int) -> str:
    lines = ["# Journal", "", JOURNAL_INTRO.format(n=keep_weeks), "", OLDER_HEADING, ""]
    lines += sorted(set(older), reverse=True) or ["(none yet)"]
    for e in sorted(entries, key=lambda e: e.day, reverse=True):
        lines += ["", e.text]
    return "\n".join(lines) + "\n"


def compact(memory_dir: Path, today: date, keep_weeks: int) -> dict[str, int]:
    """Move entries older than the kept weeks to the archive. Returns {'kept': n, 'archived': m}."""
    journal = memory_dir / "journal.md"
    archive = memory_dir / "journal_archive.md"
    older, entries = parse_journal(journal.read_text(encoding="utf-8") if journal.exists() else "")
    older = [ln for ln in older if ln != "- (none yet)"]
    limit = cutoff_date(today, keep_weeks)
    keep = [e for e in entries if e.day >= limit]
    move = [e for e in entries if e.day < limit]

    if move:
        archive_text = archive.read_text(encoding="utf-8") if archive.exists() else \
            "# Journal archive\n\nFull text of weeks removed from journal.md by `compact`. Not read by default.\n"
        _, archived = parse_journal(archive_text)
        known = {(e.day, e.text) for e in archived}
        new = [e for e in sorted(move, key=lambda e: e.day) if (e.day, e.text) not in known]
        if new:
            archive_text = archive_text.rstrip("\n") + "\n" + "".join(f"\n{e.text}\n" for e in new)
            archive.write_text(archive_text, encoding="utf-8", newline="\n")
        older += [f"- {e.day.isoformat()}: {e.summary()}" for e in move]

    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(render_journal(older, keep, keep_weeks), encoding="utf-8", newline="\n")
    return {"kept": len(keep), "archived": len(move)}
