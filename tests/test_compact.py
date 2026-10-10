"""memory/journal.md compaction: exactly N calendar weeks kept in detail, older ones archived once."""

from datetime import date, timedelta

from tradingbot.memory.compact import compact, cutoff_date, parse_journal, render_journal

TODAY = date(2026, 10, 12)       # Monday


def entry(day: date, body: str = "") -> str:
    return f"## {day.isoformat()}\n- Tóm tắt: week of {day.isoformat()}\n{body or '- details line'}\n"


def write_journal(memory, days):
    text = "# Journal\n\nintro\n\n## Older weeks\n\n(none yet)\n\n" + "\n".join(entry(d) for d in days)
    (memory / "journal.md").write_text(text, encoding="utf-8")


def mondays(n: int) -> list[date]:
    return [TODAY - timedelta(weeks=k) for k in range(n)]


def test_cutoff_is_monday_of_the_oldest_kept_week():
    assert cutoff_date(TODAY, 4) == date(2026, 9, 21)
    assert cutoff_date(date(2026, 10, 15), 4) == date(2026, 9, 21)    # Thursday, same week
    assert cutoff_date(TODAY, 1) == TODAY


def test_keeps_exactly_four_weeks_and_archives_the_rest(tmp_path):
    days = mondays(7)
    write_journal(tmp_path, days)
    assert compact(tmp_path, TODAY, 4) == {"kept": 4, "archived": 3}
    older, entries = parse_journal((tmp_path / "journal.md").read_text(encoding="utf-8"))
    assert [e.day for e in entries] == days[:4]                  # newest first
    assert older == [f"- {d.isoformat()}: week of {d.isoformat()}" for d in days[4:]]
    archive = (tmp_path / "journal_archive.md").read_text(encoding="utf-8")
    _, archived = parse_journal(archive)
    assert [e.day for e in archived] == sorted(days[4:])         # oldest first, full text
    assert "- details line" in archive


def test_boundary_monday_kept_previous_sunday_archived(tmp_path):
    cut = cutoff_date(TODAY, 4)
    write_journal(tmp_path, [cut, cut - timedelta(days=1)])
    assert compact(tmp_path, TODAY, 4) == {"kept": 1, "archived": 1}


def test_compact_is_idempotent(tmp_path):
    write_journal(tmp_path, mondays(6))
    compact(tmp_path, TODAY, 4)
    journal = (tmp_path / "journal.md").read_text(encoding="utf-8")
    archive = (tmp_path / "journal_archive.md").read_text(encoding="utf-8")
    assert compact(tmp_path, TODAY, 4) == {"kept": 4, "archived": 0}
    assert (tmp_path / "journal.md").read_text(encoding="utf-8") == journal
    assert (tmp_path / "journal_archive.md").read_text(encoding="utf-8") == archive


def test_next_week_moves_one_more_entry(tmp_path):
    write_journal(tmp_path, mondays(4))
    compact(tmp_path, TODAY, 4)
    journal = tmp_path / "journal.md"
    older, entries = parse_journal(journal.read_text(encoding="utf-8"))
    nxt = TODAY + timedelta(weeks=1)
    journal.write_text(render_journal(older, [*entries, *parse_journal(entry(nxt))[1]], 4), encoding="utf-8")
    assert compact(tmp_path, nxt, 4) == {"kept": 4, "archived": 1}
    older, entries = parse_journal(journal.read_text(encoding="utf-8"))
    assert entries[0].day == nxt and len(older) == 1
    _, archived = parse_journal((tmp_path / "journal_archive.md").read_text(encoding="utf-8"))
    assert [e.day for e in archived] == [TODAY - timedelta(weeks=3)]


def test_summary_falls_back_to_first_line(tmp_path):
    old = TODAY - timedelta(weeks=8)
    (tmp_path / "journal.md").write_text(f"# Journal\n\n## {old}\n\n- Bought AAPL, stop 253.88\n- more\n",
                                         encoding="utf-8")
    compact(tmp_path, TODAY, 4)
    older, entries = parse_journal((tmp_path / "journal.md").read_text(encoding="utf-8"))
    assert entries == [] and older == [f"- {old}: Bought AAPL, stop 253.88"]


def test_missing_journal_is_created(tmp_path):
    assert compact(tmp_path, TODAY, 4) == {"kept": 0, "archived": 0}
    assert (tmp_path / "journal.md").read_text(encoding="utf-8").startswith("# Journal")
    assert not (tmp_path / "journal_archive.md").exists()


def test_interrupted_compact_does_not_duplicate_the_archive(tmp_path):
    days = mondays(6)
    write_journal(tmp_path, days)
    compact(tmp_path, TODAY, 4)
    archive = (tmp_path / "journal_archive.md").read_text(encoding="utf-8")
    write_journal(tmp_path, days)          # crash after the archive write: journal still holds old entries
    assert compact(tmp_path, TODAY, 4) == {"kept": 4, "archived": 2}
    assert (tmp_path / "journal_archive.md").read_text(encoding="utf-8") == archive
    older, _ = parse_journal((tmp_path / "journal.md").read_text(encoding="utf-8"))
    assert len(older) == 2                 # summary lines not duplicated either
