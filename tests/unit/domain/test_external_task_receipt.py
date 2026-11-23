"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3): full text, the command apart."""
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


def test_a_queued_receipt_carries_worker_title_full_text_and_id_and_no_command():
    post, command = render(receipt_data(_created(ExternalTaskStatus.QUEUED)), strings)
    assert command is None
    assert post.startswith("Handed to *research*: *Irrigation kits*\n\n" + TEXT + "\n\n")
    assert post.endswith("Task id: `tsk-1`")


def test_the_text_is_appended_byte_for_byte_never_run_through_a_template():
    post, _ = render(receipt_data(_created(ExternalTaskStatus.QUEUED)), strings)
    assert TEXT in post


def test_a_held_receipt_says_not_sent_with_the_full_text_and_returns_the_command_alone():
    data = receipt_data(_created(ExternalTaskStatus.HELD, code="a1b2c3d4"))
    post, command = render(data, strings)
    assert post.startswith("Not sent yet:") and TEXT in post and "tsk-1" in post
    assert f"{HOLD_S // 3600} hours" in post
    assert command == "`$task send a1b2c3d4`"
    assert "a1b2c3d4" not in post


def test_a_queued_task_carries_no_command_even_with_a_code():
    assert receipt_data(_created(ExternalTaskStatus.QUEUED, code="a1b2c3d4"))["command"] is None
