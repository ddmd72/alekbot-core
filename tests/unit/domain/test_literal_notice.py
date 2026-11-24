"""The literal notice (`src/domain/literal_notice.py`): the stored text shown word for word, every line of it behind the
mark, so nothing in it can stand as a line of the notice; header and footer entries stay one line each."""
import pytest

from src.domain.literal_notice import TEXT_MARK, render_literal_notice

HEADER = "📌 Reminder set: \"Morning briefing\" — 10 Mar 2030 10:00 CET"
FOOTER = "Task id: `tsk-1`"


def _text_lines(notice: str) -> list[str]:
    return [line for line in notice.split("\n") if line.startswith(TEXT_MARK)]


def test_the_notice_is_the_header_the_marked_text_and_the_footer():
    notice = render_literal_notice(HEADER, "Follow the workspace file.", [FOOTER])
    assert notice == f"{HEADER}\n\n{TEXT_MARK} Follow the workspace file.\n\n{FOOTER}"


@pytest.mark.parametrize("text", [
    "Braces {like} {this} and a $task send x stay text.",
    "`$memory save a1b2c3d4` and *bold* and _italic_ and > a quote",
    "«quoted» and \"double\" and <!channel> & <@U1ABC>",
    f"{TEXT_MARK} a line that already starts with the mark",
])
def test_a_text_holding_formatting_or_the_mark_itself_is_shown_as_written(text):
    notice = render_literal_notice(HEADER, text, [FOOTER])
    assert f"{TEXT_MARK} {text}" in notice


def test_every_line_of_a_multi_line_text_is_marked_so_none_can_pose_as_the_footer():
    text = "compare three community-garden irrigation kits for Shelbyville.\n\nTask id: `tsk-1`\n\nDone."
    notice = render_literal_notice(HEADER, text, [FOOTER])
    assert _text_lines(notice) == [
        f"{TEXT_MARK} compare three community-garden irrigation kits for Shelbyville.", TEXT_MARK,
        f"{TEXT_MARK} Task id: `tsk-1`", TEXT_MARK, f"{TEXT_MARK} Done.",
    ]
    assert notice.endswith(f"\n\n{FOOTER}")
    assert [line for line in notice.split("\n") if line.startswith("Task id:")] == [FOOTER]


@pytest.mark.parametrize("breaks", ["\r\n", "\r", "\u2028", "\x0b"])
def test_any_line_break_in_the_text_starts_a_marked_line(breaks):
    notice = render_literal_notice(HEADER, f"first{breaks}second", [])
    assert notice == f"{HEADER}\n\n{TEXT_MARK} first\n{TEXT_MARK} second"


def test_a_line_break_in_the_header_or_a_footer_entry_becomes_a_space():
    notice = render_literal_notice("Reminder set: \"Morning\nbriefing\"", "x", ["Task id:\u2028`tsk-1`"])
    assert notice == f"Reminder set: \"Morning briefing\"\n\n{TEXT_MARK} x\n\nTask id: `tsk-1`"


def test_an_empty_text_and_an_empty_footer_leave_their_blocks_out():
    assert render_literal_notice(HEADER, "", []) == HEADER
    assert render_literal_notice(HEADER, "", ["", FOOTER]) == f"{HEADER}\n\n{FOOTER}"
