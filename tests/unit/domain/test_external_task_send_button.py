"""The Slack «Send» button on a held external task's command message (`external_task_receipt.send_button`): its binding
names the task, its one option carries the code, its prompt is the command exactly as the plain message shows it; and the
one refusal line shared by the pasted command and the button (`send_refusal_reply`)."""
import pytest

from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_receipt import (
    EXTERNAL_TASK_SEND_KIND,
    SEND_BUTTON_PLATFORMS,
    receipt_data,
    render,
    send_button,
    send_button_message,
    send_refusal_reply,
)
from src.domain.external_task_refusal import TaskRefusal
from src.domain.external_task_views import CreatedTask
from src.domain.interactive_binding import InteractiveBinding
from src.domain.ui_messages import UIMessage
from src.locales import en
from tests.support.external_task_world import NOTE

TEXT = "compare three community-garden irrigation kits for Shelbyville."


def strings(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


def _held(code="a1b2c3d4"):
    return CreatedTask(task_id="tsk-1", status=ExternalTaskStatus.HELD, label="research", title="Irrigation kits",
                       text=TEXT, note=NOTE, code=code)


def test_the_button_message_is_the_command_with_one_send_option_bound_to_the_task():
    data = receipt_data(_held())
    _, command = render(data, strings)

    message = send_button(data, "Send")

    assert message is not None
    assert message.prompt == command == "`$task send a1b2c3d4`"
    assert [(o.id, o.label) for o in message.options] == [("a1b2c3d4", "Send")]
    assert message.binding == InteractiveBinding(kind=EXTERNAL_TASK_SEND_KIND, id="tsk-1")
    assert EXTERNAL_TASK_SEND_KIND == "external_task_send"
    assert message.quote == "" and message.reply_hint == ""


def test_the_click_side_rebuilds_the_same_message_from_the_binding_and_the_option():
    posted = send_button(receipt_data(_held()), "Send")

    assert send_button_message("tsk-1", "a1b2c3d4", "Send") == posted


def test_the_task_text_and_title_never_reach_the_button_message():
    message = send_button(receipt_data(_held()), "Send")

    assert message is not None
    shown = " ".join([message.prompt, message.quote, message.reply_hint, *(o.label for o in message.options)])
    assert TEXT not in shown and "Irrigation kits" not in shown and "research" not in shown


@pytest.mark.parametrize("data", [
    receipt_data(CreatedTask(task_id="tsk-1", status=ExternalTaskStatus.QUEUED, label="research",
                             title="Irrigation kits", text=TEXT, note=NOTE)),            # no code: nothing to confirm
    {**receipt_data(_held()), "task_id": None},                                 # no task to check the owner against
    {**receipt_data(_held()), "task_id": ""},
    {**receipt_data(_held()), "command": "$memory save a1b2c3d4"},             # not the task command
    {**receipt_data(_held()), "command": 7},
    {},
])
def test_no_button_without_a_task_command_and_a_task_id(data):
    assert send_button(data, "Send") is None


def test_a_code_the_mechanism_cannot_carry_is_refused_not_mangled():
    with pytest.raises(ValueError):
        send_button_message("tsk-1", "a1 b2", "Send")
    with pytest.raises(ValueError):
        send_button_message("tsk 1", "a1b2c3d4", "Send")


def test_the_button_is_offered_on_slack_only():
    assert SEND_BUTTON_PLATFORMS == frozenset({"slack"})


@pytest.mark.parametrize("reason", [TaskRefusal.NOT_ENABLED, TaskRefusal.ACCOUNT_LOCKED])
def test_a_closed_queue_is_unavailable(reason):
    assert send_refusal_reply(reason) is UIMessage.EXTERNAL_TASK_UNAVAILABLE


@pytest.mark.parametrize("reason", [r for r in TaskRefusal
                                    if r not in (TaskRefusal.NOT_ENABLED, TaskRefusal.ACCOUNT_LOCKED)])
def test_every_other_refusal_is_the_neutral_not_found_line(reason):
    assert send_refusal_reply(reason) is UIMessage.EXTERNAL_TASK_NOT_FOUND
