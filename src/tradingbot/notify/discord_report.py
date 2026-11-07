"""Weekly report (Markdown) -> Discord embeds, readable directly in the Discord app (no download).

Discord renders bold, italics, lists, links and quotes inside embeds, but not tables, headings
or HTML comments. This module:
- cleans the Markdown: drops HTML comments, inline-code backticks and code fences, turns tables
  into bullet lines and sub-headings into bold lines;
- makes one embed per `## ` section (title = heading); the text before the first section
  (warnings, mode line) goes into a header embed titled with the `# ` title;
- splits a section longer than the embed limit on line boundaries;
- groups embeds into messages within Discord's limits (10 embeds, 6000 characters per message).
Pure functions: no network, no file access.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EMBED_TITLE_MAX = 256
EMBED_DESC_MAX = 4000          # Discord limit 4096; keep a margin
MESSAGE_EMBEDS_MAX = 10
MESSAGE_CHARS_MAX = 5800       # Discord limit 6000 (sum of titles + descriptions)

COLOR_OK = 0x2ECC71            # green: EXECUTE run, no alert
COLOR_DRY = 0xF1C40F           # yellow: DRY-RUN
COLOR_ALERT = 0xE67E22         # orange: alerts or shadow mismatch
COLOR_ABORT = 0xE74C3C         # red: run aborted / halted
COLOR_SECTION = 0x5865F2       # Discord blurple for the sections

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _table_to_bullets(rows: list[str]) -> list[str]:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    if len(cells) >= 2 and _TABLE_SEP_RE.match(rows[1]):
        header, body = cells[0], cells[2:]
    else:
        header, body = [], cells
    out = []
    for row in body:
        if not any(row):
            continue
        first, rest = row[0], row[1:]
        parts = []
        for i, value in enumerate(rest, start=1):
            if not value or value in ("-", "–"):
                continue
            label = header[i] if i < len(header) and header[i] else ""
            parts.append(f"{label}: {value}" if label else value)
        out.append(f"- **{first}**" + (" — " + "; ".join(parts) if parts else ""))
    return out


def clean_markdown(text: str) -> str:
    """Markdown -> what Discord renders well (see module docstring)."""
    text = _COMMENT_RE.sub("", text)
    lines_in = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines_in):
        line = lines_in[i]
        if line.lstrip().startswith("|"):
            block = []
            while i < len(lines_in) and lines_in[i].lstrip().startswith("|"):
                block.append(lines_in[i])
                i += 1
            out.extend(_table_to_bullets(block))
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            i += 1
            continue
        heading = re.match(r"^#{3,6}\s+(.*)$", stripped)
        if heading:
            line = f"**{heading.group(1).strip()}**"
        out.append(line.replace("`", ""))
        i += 1
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_text(text: str, limit: int = EMBED_DESC_MAX) -> list[str]:
    """Split on line boundaries so every chunk is <= limit (a single huge line is hard-cut)."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c.strip() for c in chunks if c.strip()] or [""]


def split_sections(markdown: str) -> tuple[str, str, list[tuple[str, str]]]:
    """-> (title, intro text, [(section heading, section text), ...]) after cleaning."""
    title, intro_lines = "", []
    sections: list[tuple[str, list[str]]] = []
    for line in markdown.splitlines():
        h1 = re.match(r"^#\s+(.*)$", line.strip())
        h2 = re.match(r"^##\s+(.*)$", line.strip())
        if h1 and not title and not sections:
            title = h1.group(1).strip()
        elif h2:
            sections.append((h2.group(1).strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            intro_lines.append(line)
    return (title.replace("`", ""), clean_markdown("\n".join(intro_lines)),
            [(h.replace("`", ""), clean_markdown("\n".join(body))) for h, body in sections])


def status_color(summary: Mapping[str, Any] | None) -> int:
    if not summary:
        return COLOR_SECTION
    if summary.get("status") == "aborted":
        return COLOR_ABORT
    if summary.get("alerts") or (summary.get("shadow_check") or {}).get("match") is False:
        return COLOR_ALERT
    if summary.get("mode") == "DRY-RUN":
        return COLOR_DRY
    return COLOR_OK


def headline(summary: Mapping[str, Any] | None, title: str) -> str:
    """One short line for the message text (shown in the phone's push notification)."""
    if not summary:
        return f"📊 {title}"
    if summary.get("status") == "aborted":
        state = "🔴 lần chạy bị dừng"
    elif summary.get("mode") == "DRY-RUN":
        state = "🟡 chạy thử, không gửi lệnh"
    else:
        state = "🟢 đã chạy trên tài khoản paper"
    alerts = summary.get("alerts") or []
    extra = f" · ⚠️ {len(alerts)} cảnh báo" if alerts else ""
    return f"📊 **{title}** — {state}{extra}"


def build_report_messages(markdown: str, summary: Mapping[str, Any] | None = None,
                          ) -> list[dict[str, Any]]:
    """Report Markdown -> list of Discord message payload parts: {"content": str, "embeds": [...]}."""
    title, intro, sections = split_sections(markdown)
    title = title or "Báo cáo tuần"
    embeds: list[dict[str, Any]] = []
    for k, chunk in enumerate(_split_text(intro)):
        if not chunk:
            continue                     # no intro text: the title is in the message headline
        embeds.append({"title": (title if k == 0 else f"{title} (tiếp)")[:EMBED_TITLE_MAX],
                       "description": chunk or "​", "color": status_color(summary)})
    for heading, body in sections:
        for k, chunk in enumerate(_split_text(body or "Không có.")):
            embeds.append({"title": (heading if k == 0 else f"{heading} (tiếp)")[:EMBED_TITLE_MAX],
                           "description": chunk or "​", "color": COLOR_SECTION})
    messages: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for embed in embeds:
        n = len(embed["title"]) + len(embed["description"])
        if current and (len(current) >= MESSAGE_EMBEDS_MAX or size + n > MESSAGE_CHARS_MAX):
            messages.append({"content": "", "embeds": current})
            current, size = [], 0
        current.append(embed)
        size += n
    if current:
        messages.append({"content": "", "embeds": current})
    if not messages:
        messages.append({"content": "", "embeds": []})
    messages[0]["content"] = headline(summary, title)
    return messages
