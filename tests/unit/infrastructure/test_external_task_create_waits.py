"""A task created through Smart's tool always waits for the owner's confirmation (decision record
external_task_confirmation_and_alerts): the stored task is held, the model reads a pending note without the code, and
the owner's receipt carries the full text and the command. Alex Doe is `usr-1` with the worker `research`."""
import pytest

from src.domain.external_task import HOLD_S, ExternalTaskStatus
from src.domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from src.domain.llm import ToolCall
from src.infrastructure.external_task_tools import make_create_handler
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld


@pytest.fixture
async def world():
    return await ExternalTaskWorld().seed(("research", "calendar-watch"))


async def test_a_create_is_held_and_the_model_reads_a_pending_note_with_no_code(world):
    handler = make_create_handler(world.service, USER_ID, ORIGIN)
    result = await handler(ToolCall(name="create_external_task",
                                    args={"worker": "research", "title": TITLE, "text": TEXT}))
    [task] = world.tasks.tasks.values()
    assert task.status is ExternalTaskStatus.HELD and task.hold is not None
    assert "waits for the owner's confirmation" in result.result_str and f"{HOLD_S // 3600} hours" in result.result_str
    [item] = result.delivery_items
    assert item.type == EXTERNAL_TASK_RECEIPT_DELIVERY and item.data["text"] == TEXT
    code = item.data["command"].split()[-1]
    assert code not in result.result_str and "queued" not in result.result_str


async def test_two_creates_in_one_handler_are_both_held(world):
    handler = make_create_handler(world.service, USER_ID, ORIGIN)
    for title in ("first", "second"):
        await handler(ToolCall(name="create_external_task", args={"worker": "research", "title": title, "text": TEXT}))
    assert [t.status for t in world.tasks.tasks.values()] == [ExternalTaskStatus.HELD] * 2
