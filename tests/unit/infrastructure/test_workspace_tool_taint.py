"""Workspace writes of a tainted execution are flagged, and a flagged file read back taints the next execution
(EXTERNAL_TASK_QUEUE_RFC §5.3, §6.1).

The real WorkspaceService over the GCS fake, the real TaintTracker and the external task world: what is stored, what the
model is told and whether a later `create_external_task` is queued or held. Alex Doe is `usr-1`.
"""
import pytest

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.llm import ToolCall
from src.domain.workspace import TAINTED_EXECUTION_MARK, WorkspaceReferences
from src.domain.external_task import ExternalTaskStatus
from src.infrastructure.external_task_tools import make_create_handler
from src.infrastructure.taint_tracker import TaintTracker
from src.infrastructure.workspace_tools import make_workspace_handler
from src.services.workspace_service import WorkspaceService
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld
from tests.support.gcs_fake import FakeGcs

BUCKET = "workspace-test"
PATH = "projects/irrigation.md"
BODY = "community-garden irrigation kits for Shelbyville\n"


class Rig:
    def __init__(self) -> None:
        self.gcs = FakeGcs(buckets={BUCKET: "versioned"})
        self.store = GcsWorkspaceAdapter(BUCKET, client=self.gcs.client())
        self.service = WorkspaceService(self.store, nonce=lambda: "nonce00000001")

    def handler(self, tracker=None):
        is_tainted = None if tracker is None else (lambda: tracker.tainted)
        return make_workspace_handler(self.service, USER_ID, "chat", None, WorkspaceReferences(), is_tainted=is_tainted)

    async def marks(self):
        return [e.untrusted for e in await self.store.list(USER_ID, PATH)]


def call(**args):
    return ToolCall(name="workspace", args=args)


@pytest.fixture
def rig():
    return Rig()


async def test_a_write_in_a_tainted_execution_is_flagged_and_an_untainted_one_is_not(rig):
    tracker = TaintTracker()
    handler = rig.handler(tracker)
    await handler(call(command="create", path="projects/clean.md", content=BODY))
    tracker.mark()
    await handler(call(command="create", path=PATH, content=BODY))
    assert [e.untrusted for e in await rig.store.list(USER_ID, "projects/clean.md")] == [None]
    assert await rig.marks() == [TAINTED_EXECUTION_MARK]


async def test_a_handler_with_no_callable_is_never_tainted(rig):
    await rig.handler()(call(command="create", path=PATH, content=BODY))
    assert await rig.marks() == [None]


async def test_every_write_command_of_a_tainted_call_carries_the_mark_and_a_flagged_file_keeps_its_own(rig):
    clean = rig.handler(TaintTracker())
    await clean(call(command="create", path="projects/a.md", content="one\n"))
    await clean(call(command="create", path="projects/own.md", content="two\n"))
    tainted = TaintTracker()
    tainted.mark()
    handler = rig.handler(tainted)
    await handler(call(command="view", path="projects/a.md"))
    await handler(call(command="str_replace", path="projects/a.md", old_str="one", new_str="uno"))
    await handler(call(command="insert", path="projects/a.md", insert_line=-1, insert_text="three"))
    await handler(call(command="copy", path="projects/b.md", source="workspace:projects/a.md"))
    await handler(call(command="rename", path="projects/b.md", new_path="projects/c.md"))
    marks = {p: [e.untrusted for e in await rig.store.list(USER_ID, p)] for p in ("projects/a.md", "projects/c.md")}
    assert marks == {"projects/a.md": [TAINTED_EXECUTION_MARK], "projects/c.md": [TAINTED_EXECUTION_MARK]}
    # a file that already carries a provenance keeps it
    await rig.service.write_marked(USER_ID, "references/external.md", "x", origin="t", mark="external_task:abcd")
    await handler(call(command="view", path="references/external.md"))
    await handler(call(command="str_replace", path="references/external.md", old_str="x", new_str="y"))
    assert [e.untrusted for e in await rig.store.list(USER_ID, "references/external.md")] == ["external_task:abcd"]


async def test_a_later_view_is_wrapped_sets_the_signal_and_holds_a_create():
    world = await ExternalTaskWorld().seed()
    rig = Rig()
    writer = TaintTracker()
    writer.mark()                              # the earlier execution had read a web page
    await rig.handler(writer)(call(command="create", path=PATH, content=BODY))

    reader = TaintTracker()                    # a fresh execution reads the file back
    handler = rig.handler(reader)
    view = ToolCall(name="workspace", args={"command": "view", "path": PATH})
    result = await handler(view)
    assert result.untrusted is True and "<external_content" in result.result_str
    assert "community-garden irrigation kits" in result.result_str

    reader.observe_batch([view], [result])
    assert reader.tainted is True
    create = make_create_handler(world.service, USER_ID, ORIGIN)
    await create(call_create())
    [task] = world.tasks.tasks.values()
    assert task.status is ExternalTaskStatus.HELD


async def test_viewing_an_unflagged_file_leaves_the_execution_clean_and_the_create_queued():
    world = await ExternalTaskWorld().seed()
    rig = Rig()
    await rig.handler()(call(command="create", path=PATH, content=BODY))
    reader = TaintTracker()
    view = ToolCall(name="workspace", args={"command": "view", "path": PATH})
    result = await rig.handler(reader)(view)
    assert result.untrusted is False and "external_content" not in result.result_str
    reader.observe_batch([view], [result])
    assert reader.tainted is False
    await make_create_handler(world.service, USER_ID, ORIGIN)(call_create())
    assert next(iter(world.tasks.tasks.values())).status is ExternalTaskStatus.QUEUED


def call_create():
    return ToolCall(name="create_external_task", args={"worker": "research", "title": TITLE, "text": TEXT})
