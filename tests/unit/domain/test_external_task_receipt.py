"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3): full text in the file, the command apart.

The receipt's shape as a literal notice (the file name, the message as header plus id line, a text that writes an id
line staying in the file) is pinned in `tests/unit/domain/test_literal_notice_file.py`."""
from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_receipt import render, receipt_data
from src.domain.external_task_views import CreatedTask
from src.domain.ui_messages import UIMessage
from src.locales import en

TEXT = "compare three community-garden irrigation kits for Shelbyville.\n\nBraces {like} {this} and a $task send x stay text."


def strings(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


def _created(status, code=None):
    return CreatedTask(task_id="tsk-1", status=status, label="research", title="Irrigation kits", text=TEXT, code=code)


def test_the_text_is_filed_byte_for_byte_never_run_through_a_template():
    notice, _ = render(receipt_data(_created(ExternalTaskStatus.QUEUED)), strings)
    assert notice.content == TEXT.encode("utf-8")   # no template ate the braces, no line was cut or marked
    assert notice.filename == "task-tsk-1.txt"


def test_a_held_receipt_says_it_waits_for_confirmation_with_the_full_text_and_returns_the_command_alone():
    data = receipt_data(_created(ExternalTaskStatus.HELD, code="a1b2c3d4"))
    notice, command = render(data, strings)
    assert notice.message.startswith("Waiting for your confirmation: a task for *research*: *Irrigation kits*.")
    assert notice.content == TEXT.encode("utf-8") and notice.message.endswith("Task id: `tsk-1`")
    assert f"{HOLD_S // 3600} hours" in notice.message
    assert command == "`$task send a1b2c3d4`"
    assert "a1b2c3d4" not in notice.message and "a1b2c3d4" not in notice.text
