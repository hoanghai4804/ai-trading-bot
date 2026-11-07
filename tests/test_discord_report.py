"""Report Markdown -> Discord embeds: cleaning, sections, Discord limits, colors. Pure, no network."""

from pathlib import Path

from tradingbot.notify.discord import DiscordNotifier
from tradingbot.notify.discord_report import (
    COLOR_ABORT, COLOR_ALERT, COLOR_DRY, COLOR_OK, EMBED_DESC_MAX, MESSAGE_CHARS_MAX, MESSAGE_EMBEDS_MAX,
    build_report_messages, clean_markdown, split_sections, status_color,
)
from tests.test_notify import CFG, URL, FakePost

REPORT = """# 📊 Báo cáo tuần 02/11/2026

> 🟢 **Chế độ thật trên tài khoản giả lập (paper)**

## 📌 Tóm tắt
- Không có lệnh `orders_sent=0`.
<!-- a template comment that was left behind -->

## 💼 Tài khoản
| Mã | SL | Giá vốn |
|---|---|---|
| GOOGL | 14 | 345.97 |

### Chi tiết
```
raw block
```
- xong
"""


def test_clean_markdown_removes_code_symbols_comments_and_tables():
    text = clean_markdown("a `x=1` b\n<!-- hidden\nmultiline -->\n| A | B |\n|---|---|\n| X | 1 |\n### Sub\n```\ncode\n```")
    assert "`" not in text and "hidden" not in text and "|" not in text and "###" not in text
    assert "a x=1 b" in text and "- **X** — B: 1" in text and "**Sub**" in text and "code" in text


def test_table_without_header_and_empty_cells():
    assert clean_markdown("| AAPL | | 3 |") == "- **AAPL** — 3"


def test_split_sections_title_intro_and_headings():
    title, intro, sections = split_sections(REPORT)
    assert title == "📊 Báo cáo tuần 02/11/2026"
    assert intro.startswith("> 🟢")
    assert [h for h, _ in sections] == ["📌 Tóm tắt", "💼 Tài khoản"]
    assert sections[0][1] == "- Không có lệnh orders_sent=0."
    assert "- **GOOGL** — SL: 14; Giá vốn: 345.97" in sections[1][1]


def test_build_messages_header_then_one_embed_per_section():
    [msg] = build_report_messages(REPORT, {"mode": "EXECUTE", "alerts": []})
    titles = [e["title"] for e in msg["embeds"]]
    assert titles == ["📊 Báo cáo tuần 02/11/2026", "📌 Tóm tắt", "💼 Tài khoản"]
    assert msg["embeds"][0]["color"] == COLOR_OK
    assert "đã chạy trên tài khoản paper" in msg["content"]
    assert all("`" not in e["description"] for e in msg["embeds"])


def test_long_section_is_split_and_messages_respect_discord_limits():
    body = "\n".join(f"- dòng {i} " + "x" * 80 for i in range(400))      # ~36k chars
    sections = "\n".join(f"## Phần {k}\n{body}" for k in range(3))
    messages = build_report_messages("# T\n\nintro\n" + sections, None)
    embeds = [e for m in messages for e in m["embeds"]]
    assert len(embeds) > 3 and any(e["title"].endswith("(tiếp)") for e in embeds)
    for m in messages:
        assert len(m["embeds"]) <= MESSAGE_EMBEDS_MAX
        assert sum(len(e["title"]) + len(e["description"]) for e in m["embeds"]) <= MESSAGE_CHARS_MAX
        assert all(len(e["description"]) <= EMBED_DESC_MAX for e in m["embeds"])
    joined = "\n".join(e["description"] for e in embeds)
    assert joined.count("- dòng 399 ") == 3                               # nothing lost
    assert messages[0]["content"] and all(m["content"] == "" for m in messages[1:])


def test_a_single_huge_line_is_hard_cut():
    messages = build_report_messages("# T\n## S\n" + "y" * 9000, None)
    assert all(len(e["description"]) <= EMBED_DESC_MAX for m in messages for e in m["embeds"])


def test_status_colors():
    assert status_color({"status": "aborted", "mode": "EXECUTE"}) == COLOR_ABORT
    assert status_color({"mode": "EXECUTE", "alerts": ["x"]}) == COLOR_ALERT
    assert status_color({"mode": "EXECUTE", "shadow_check": {"match": False}}) == COLOR_ALERT
    assert status_color({"mode": "DRY-RUN"}) == COLOR_DRY
    assert status_color({"mode": "EXECUTE"}) == COLOR_OK


def test_headline_for_aborted_run_and_alert_count():
    [msg] = build_report_messages("# T\n## S\nx", {"status": "aborted", "alerts": ["a", "b"]})
    assert "bị dừng" in msg["content"] and "2 cảnh báo" in msg["content"]


def test_notifier_sends_embeds_without_attachment(tmp_path: Path):
    post = FakePost(204, 204)
    n = DiscordNotifier(URL, CFG.notify, log_path=tmp_path / "notify.log", post=post, sleep=lambda s: None)
    messages = [{"content": "head", "embeds": [{"title": "A", "description": "a"}]},
                {"content": "", "embeds": [{"title": "B", "description": "b"}]}]
    assert n.send_report(messages)
    payloads = [kw["json"] for _, kw in post.calls]
    assert [p["embeds"][0]["title"] for p in payloads] == ["A", "B"]
    assert payloads[0]["content"] == "head" and all("files" not in kw for _, kw in post.calls)
    assert all(p["allowed_mentions"] == {"parse": []} for p in payloads)


def test_send_report_reports_failure_but_keeps_sending(tmp_path: Path):
    post = FakePost(400, 204)
    n = DiscordNotifier(URL, CFG.notify, log_path=tmp_path / "notify.log", post=post, sleep=lambda s: None)
    msgs = [{"content": "a", "embeds": [{"title": "A", "description": "a"}]},
            {"content": "", "embeds": [{"title": "B", "description": "b"}]}]
    assert n.send_report(msgs) is False and len(post.calls) == 2
