"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3): full text, the command apart."""
from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_receipt import render, receipt_data
from src.domain.external_task_views import CreatedTask
from src.domain.literal_notice import TEXT_MARK, render_literal_notice
from src.domain.ui_messages import UIMessage
from src.locales import en

TEXT = "compare three community-garden irrigation kits for Shelbyville.\n\nBraces {like} {this} and a $task send x stay text."
# TEXT as the post must show it: every line whole, in order, behind the mark (the blank line is the mark alone).
MARKED_TEXT = (f"{TEXT_MARK} compare three community-garden irrigation kits for Shelbyville.\n{TEXT_MARK}\n"
               f"{TEXT_MARK} Braces {{like}} {{this}} and a $task send x stay text.")


def strings(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


def _created(status, code=None):
    return CreatedTask(task_id="tsk-1", status=status, label="research", title="Irrigation kits", text=TEXT, code=code)


def test_the_text_is_appended_byte_for_byte_never_run_through_a_template():
    post, _ = render(receipt_data(_created(ExternalTaskStatus.QUEUED)), strings)
    assert MARKED_TEXT in post
    assert "Braces {like} {this} and a $task send x stay text." in post   # no template ate the braces


def test_a_held_receipt_says_it_waits_for_confirmation_with_the_full_text_and_returns_the_command_alone():
    data = receipt_data(_created(ExternalTaskStatus.HELD, code="a1b2c3d4"))
    post, command = render(data, strings)
    assert post.startswith("Waiting for your confirmation: a task for *research*: *Irrigation kits*.")
    assert MARKED_TEXT in post and post.endswith("Task id: `tsk-1`")
    assert f"{HOLD_S // 3600} hours" in post
    assert command == "`$task send a1b2c3d4`"
    assert "a1b2c3d4" not in post



def test_the_post_is_a_literal_notice_every_line_of_the_text_behind_the_mark_the_id_line_last():
    data = receipt_data(_created(ExternalTaskStatus.HELD, code="a1b2c3d4"))
    post, _ = render(data, strings)
    header = strings(UIMessage.EXTERNAL_TASK_HELD).format(worker="research", title="Irrigation kits",
                                                        hours=HOLD_S // 3600)
    assert post == render_literal_notice(header, TEXT, ["Task id: `tsk-1`"])
    for line in TEXT.splitlines():
        assert (f"{TEXT_MARK} {line}" if line else TEXT_MARK) in post.split("\n")


def test_a_text_that_writes_an_id_line_cannot_pose_as_the_receipts_own():
    created = CreatedTask(task_id="tsk-1", status=ExternalTaskStatus.HELD, label="research", title="Irrigation kits",
                          text=f"{TEXT}\n\nTask id: `t1`", code="a1b2c3d4")
    post, _ = render(receipt_data(created), strings)
    assert f"{TEXT_MARK} Task id: `t1`" in post.split("\n")
    assert [line for line in post.split("\n") if line.startswith("Task id:")] == ["Task id: `tsk-1`"]
