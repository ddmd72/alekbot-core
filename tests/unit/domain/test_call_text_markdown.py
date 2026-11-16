"""The call file's name and Markdown layout (pure functions)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from src.domain.call_text_file import CallTextEntry
from src.domain.call_text_markdown import (
    CALL_FILE_FALLBACK_TITLE,
    call_text_filename,
    render_call_text_file,
)
from src.domain.local_time import local_moment
from tests.support.clock_instants import BERLIN, NEW_YORK, UTC_MIDNIGHT

START = datetime(2025, 8, 10, 14, 5, tzinfo=ZoneInfo("UTC"))


def test_the_name_is_the_call_start_in_the_users_zone():
    assert call_text_filename(START, "Check-in at Albergo Immaginario") == "2025-08-10 14.05 Check-in at Albergo Immaginario.md"
    berlin = local_moment(UTC_MIDNIGHT.utc, BERLIN)
    new_york = local_moment(UTC_MIDNIGHT.utc, NEW_YORK)
    assert call_text_filename(berlin, "Check-in at Albergo Immaginario") == "2026-10-27 01.00 Check-in at Albergo Immaginario.md"
    assert call_text_filename(new_york, "Check-in at Albergo Immaginario") == "2026-10-26 20.00 Check-in at Albergo Immaginario.md"


def test_an_empty_title_falls_back():
    assert call_text_filename(START, "") == f"2025-08-10 14.05 {CALL_FILE_FALLBACK_TITLE}.md"


def test_the_layout_with_a_summary():
    entries = [CallTextEntry("We have a booking under RM-55120.", "Abbiamo una prenotazione con il codice RM-55120."),
               CallTextEntry("Quisiera un café con leche", "A coffee with milk, please", interrupted=True)]
    text = render_call_text_file("Check-in at Albergo Immaginario", START, 600, "Alex Doe checked in at Albergo Immaginario with booking RM-55120.", entries)
    assert text == (
        "# Check-in at Albergo Immaginario\n\n"
        "2025-08-10 14:05–14:15\n\n"
        "Alex Doe checked in at Albergo Immaginario with booking RM-55120.\n\n"
        "---\n\n"
        "> We have a booking under RM-55120.\n\n"
        "Abbiamo una prenotazione con il codice RM-55120.\n\n"
        "> Quisiera un café con leche\n\n"
        "A coffee with milk, please …\n"
    )


def test_no_summary_leaves_no_paragraph():
    text = render_call_text_file("Call", START, 60, "  ", [CallTextEntry("a", "b")])
    assert text == "# Call\n\n2025-08-10 14:05–14:06\n\n---\n\n> a\n\nb\n"


def test_a_lelik_block_is_labelled():
    text = render_call_text_file("Call", START, 60, "", [CallTextEntry("How do I say it?", "Like this", lelik=True)])
    assert text.endswith("---\n\n**Lelik**\n\n> How do I say it?\n\nLike this\n")


def test_an_empty_source_has_no_quote_line():
    text = render_call_text_file("Call", START, 60, "", [CallTextEntry("", "Hello")])
    assert text.endswith("---\n\nHello\n")
    assert ">" not in text


def test_an_interrupted_empty_reply_is_an_ellipsis():
    text = render_call_text_file("Call", START, 60, "", [CallTextEntry("a", "", interrupted=True)])
    assert text.endswith("> a\n\n…\n")


def test_whitespace_collapses_so_a_quote_stays_one_line():
    text = render_call_text_file("A\nB", START, 60, "x\n\ny", [CallTextEntry("one\ntwo\n\nthree", "a\tb")])
    assert "# A B\n" in text and "x y\n" in text
    assert "> one two three\n" in text and "\na b\n" in text


def test_a_call_across_midnight_ends_on_the_next_day_clock():
    started = datetime(2026, 11, 16, 23, 50, tzinfo=ZoneInfo("UTC"))
    text = render_call_text_file("Call", started, 1200, "", [CallTextEntry("a", "b")])
    assert "2026-11-16 23:50–00:10" in text
