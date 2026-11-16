"""
The call's text as one Markdown file (VOICE_CALL_LIVE_TEXT_RFC §4.11): pure functions, no localized words.

Layout: `# <title>`, the call's local time span, the summary paragraph if there is one, `---`, then each entry as a
quoted `> <source>` line and the reply under it (` …` after an interrupted one); a Lelik block is preceded by a
`**Lelik**` line. Whitespace inside a text collapses, so a quote stays one line.
"""
from datetime import datetime, timedelta
from typing import Sequence

from .call_text_file import CallTextEntry
from .user_drive import stamped_drive_filename

CALL_FILE_EXTENSION = ".md"
CALL_FILE_CONTENT_TYPE = "text/markdown; charset=utf-8"
CALL_FILE_FALLBACK_TITLE = "Call"


def call_text_filename(started: datetime, title: str) -> str:
    """`YYYY-MM-DD HH.MM <title>.md`, the time being the call's start in the user's zone."""
    return stamped_drive_filename(started.strftime("%Y-%m-%d %H.%M"), title or CALL_FILE_FALLBACK_TITLE,
                                  CALL_FILE_EXTENSION)


def _line(text: str) -> str:
    return " ".join(text.split())


def render_call_text_file(title: str, started: datetime, duration_s: float, summary: str,
                          entries: Sequence[CallTextEntry]) -> str:
    ended = started + timedelta(seconds=duration_s)
    span = f"{started.strftime('%Y-%m-%d %H:%M')}–{ended.strftime('%H:%M')}"
    parts = [f"# {_line(title) or CALL_FILE_FALLBACK_TITLE}", "", span, ""]
    if _line(summary):
        parts += [_line(summary), ""]
    parts += ["---", ""]
    for entry in entries:
        if entry.lelik:
            parts += ["**Lelik**", ""]
        if _line(entry.source):
            parts += [f"> {_line(entry.source)}", ""]
        reply = _line(entry.text)
        if entry.interrupted:
            reply = f"{reply} …" if reply else "…"
        if reply:
            parts += [reply, ""]
    return "\n".join(parts)
