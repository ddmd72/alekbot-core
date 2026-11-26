"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3): full text in the file, the command apart.

The receipt's shape as a literal notice (the file name, the message as header plus id line, a text that writes an id
line staying in the file) is pinned in `tests/unit/domain/test_literal_notice_file.py`."""
from src.domain.external_task import HOLD_S, ExternalTaskStatus, TaskFile
from src.domain.external_task_receipt import render, receipt_data
from src.domain.external_task_views import CreatedTask
from src.domain.ui_messages import UIMessage
from src.locales import en
from tests.support.external_task_world import NOTE
from tests.support.task_receipt_file import receipt_parts

TEXT = "compare three community-garden irrigation kits for Shelbyville.\n\nBraces {like} {this} and a $task send x stay text."


def strings(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


def _created(status, code=None):
    return CreatedTask(task_id="tsk-1", status=status, label="research", title="Irrigation kits", text=TEXT, note=NOTE,
                       code=code)


def test_the_text_is_filed_byte_for_byte_never_run_through_a_template():
    notice, _ = render(receipt_data(_created(ExternalTaskStatus.QUEUED)), strings)
    front, body = receipt_parts(notice.content)
    assert body == TEXT.encode("utf-8")   # no template ate the braces, no line was cut or marked
    assert front == {"title": "Irrigation kits", "worker": "research", "note": NOTE}
    assert notice.filename == "task-tsk-1.txt"


def test_a_held_receipt_says_it_waits_for_confirmation_with_the_full_text_and_returns_the_command_alone():
    data = receipt_data(_created(ExternalTaskStatus.HELD, code="a1b2c3d4"))
    notice, command = render(data, strings)
    assert notice.message.startswith("Waiting for your confirmation: a task for *research*: *Irrigation kits*.")
    front, body = receipt_parts(notice.content)
    assert body == TEXT.encode("utf-8") and notice.message.endswith("Task id: `tsk-1`")
    assert front == {"title": "Irrigation kits", "worker": "research", "note": NOTE}
    assert f"{HOLD_S // 3600} hours" in notice.message
    assert command == "`$task send a1b2c3d4`"
    assert "a1b2c3d4" not in notice.message and "a1b2c3d4" not in notice.text


def test_the_receipt_lists_each_attached_file_by_name_and_size_after_the_note():
    files = (TaskFile(number=1, name="Tom's plan.docx", mime="application/vnd.ms-word", size=2048,
                      key="external_tasks/usr-1/tsk-1/1-Tom's plan.docx"),
             TaskFile(number=2, name="birth-certificate.pdf", mime="application/pdf", size=3 * 1024 * 1024,
                      key="external_tasks/usr-1/tsk-1/2-birth-certificate.pdf"))
    created = CreatedTask(task_id="tsk-1", status=ExternalTaskStatus.HELD, label="research", title="Plan review",
                          text=TEXT, note=NOTE, code="a1b2c3d4", files=files)
    data = receipt_data(created)
    assert data["files"] == [{"name": "Tom's plan.docx", "size": 2048},
                             {"name": "birth-certificate.pdf", "size": 3 * 1024 * 1024}]
    notice, _ = render(data, strings)
    front, body = receipt_parts(notice.content)
    assert list(front.items()) == [("title", "Plan review"), ("worker", "research"), ("note", NOTE),
                                   ("file_1", "Tom's plan.docx (2.0 KB)"), ("file_2", "birth-certificate.pdf (3.0 MB)")]
    assert body == TEXT.encode("utf-8")
    assert "external_tasks/" not in notice.content.decode("utf-8")      # the storage key is never shown


def test_a_receipt_without_files_keeps_the_front_matter_unchanged():
    data = receipt_data(_created(ExternalTaskStatus.QUEUED))
    assert "files" not in data
    front, _ = receipt_parts(render(data, strings)[0].content)
    assert list(front) == ["title", "worker", "note"]
