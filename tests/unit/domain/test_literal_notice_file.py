"""The literal notice as a file (`src/domain/literal_notice.py`) and its three producers' domain renderers: the exact text
goes byte for byte into the file; the message holds the header and the short lines, one line each."""
import pytest

from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_receipt import receipt_data, render
from src.domain.external_task_views import CreatedTask
from src.domain.literal_notice import render_literal_notice
from src.domain.memory_fact import MemoryFactDraft, ProposedFact, render_preview_file, render_preview_notice
from src.domain.ui_messages import UIMessage
from src.locales import en

TEXT = "compare three community-garden irrigation kits for Shelbyville.\n\nBraces {like} {this} and a $task send x stay text."


def strings(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


@pytest.mark.parametrize("text", [
    TEXT,
    "`$memory save a1b2c3d4` and *bold* and _italic_ and > a quote",
    "«quoted» and \"double\" and <!channel> & <@U1ABC>",
    "│ a line that starts with the old mark\r\nTask id: `t1`\u2028Done.",
])
def test_the_file_holds_the_text_byte_for_byte(text):
    notice = render_literal_notice("📌 Reminder set: \"Morning briefing\"", text, filename="reminder.txt",
                                   title="Morning briefing")
    assert notice.text == text and notice.content == text.encode("utf-8")
    assert text not in notice.message


def test_the_message_is_the_header_then_each_line_and_line_breaks_in_them_become_spaces():
    notice = render_literal_notice("Reminder set: \"Morning\nbriefing\"", "x", filename="reminder.txt",
                                   title="Morning\nbriefing", lines=["", "10 Mar 2030 10:00 CET\u2028· workspace file"])
    assert notice.message == "Reminder set: \"Morning briefing\"\n10 Mar 2030 10:00 CET · workspace file"
    assert notice.title == "Morning briefing" and notice.filename == "reminder.txt"


def test_no_lines_leave_the_header_alone_and_an_empty_text_is_an_empty_file():
    header = "📌 Reminder set: \"Morning briefing\""
    notice = render_literal_notice(header, "", filename="reminder.txt", title="Morning briefing")
    assert notice.message == header and notice.content == b""
    with_lines = render_literal_notice(header, "", filename="reminder.txt", title="Morning briefing",
                                       lines=["", "Task id: `tsk-1`"])
    assert with_lines.message == f"{header}\nTask id: `tsk-1`"


def _held(task_id="tsk-1", text=TEXT):
    return receipt_data(CreatedTask(task_id=task_id, status=ExternalTaskStatus.HELD, label="research",
                                    title="Irrigation kits", text=text, code="a1b2c3d4"))


def test_the_task_receipt_is_the_text_in_its_file_then_the_header_and_the_id_line():
    notice, command = render(_held(), strings)
    header = strings(UIMessage.EXTERNAL_TASK_HELD).format(worker="research", title="Irrigation kits",
                                                        hours=HOLD_S // 3600)
    assert notice.filename == "task-tsk-1.txt" and notice.title == "Irrigation kits"
    assert notice.content == TEXT.encode("utf-8")
    assert notice.message == f"{header}\nTask id: `tsk-1`"
    assert command == "`$task send a1b2c3d4`" and "a1b2c3d4" not in notice.message


def test_a_task_text_that_writes_an_id_line_stays_in_the_file():
    notice, _ = render(_held(text=f"{TEXT}\n\nTask id: `t1`"), strings)
    assert "t1`" not in notice.message and notice.text.endswith("Task id: `t1`")


@pytest.mark.parametrize("task_id, filename", [("../tsk 1", "task-tsk1.txt"), ("", "task-text.txt")])
def test_the_task_file_name_keeps_only_safe_characters_of_the_id(task_id, filename):
    data = {**_held(), "task_id": task_id}
    assert render(data, strings)[0].filename == filename


def test_the_memory_preview_is_the_facts_file_then_the_count_line():
    draft = MemoryFactDraft(facts=[ProposedFact(text="User's passport expires on 2031-03-04.", source="passport.pdf")])
    notice = render_preview_notice(draft, strings)
    assert notice.filename == "memory-facts.txt"
    assert notice.content == render_preview_file(draft).encode("utf-8")
    assert notice.title == strings(UIMessage.MEMORY_PREVIEW_FILE_TITLE).format(count=1)
    assert notice.message == strings(UIMessage.MEMORY_PREVIEW_HEADER).format(count=1)
