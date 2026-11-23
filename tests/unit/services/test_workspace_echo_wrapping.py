"""The lines a write echoes back from a flagged file are outside content like any read of it (EXTERNAL_TASK_QUEUE_RFC
§6.1): str_replace and insert wrap the snippet and set `WorkspaceResult.untrusted`; restore and history print no file
bytes; an unflagged file's echo is unchanged. The real WorkspaceService over the GCS fake.
"""
import pytest

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.untrusted_text import EXTERNAL_REMINDER
from src.domain.workspace import WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.support.gcs_fake import FakeGcs

USER = "usr-1"
FLAGGED = "references/irrigation.md"
PLAIN = "projects/irrigation.md"
MARK = "external_task:a1b2c3d4-0000"
NONCE = "nonce00000001"
OPEN = f'<external_content nonce="{NONCE}" ref="workspace:{FLAGGED}" name="{FLAGGED}">'
BODY = "Kit A costs 40 USD.\nKit B costs 55 USD.\n"


class World:
    def __init__(self) -> None:
        self.gcs = FakeGcs(buckets={"workspace-test": "versioned"})
        self.store = GcsWorkspaceAdapter("workspace-test", client=self.gcs.client())
        self.service = WorkspaceService(self.store, nonce=lambda: NONCE)

    async def run(self, command, **args):
        return await self.service.run(WorkspaceCall(user_id=USER, origin="chat", known={}), command, args)


@pytest.fixture
async def world():
    w = World()
    await w.service.write_marked(USER, FLAGGED, BODY, origin="t", mark=MARK)
    await w.run("create", path=PLAIN, content=BODY)
    return w


async def test_a_str_replace_echo_of_a_flagged_file_is_wrapped(world):
    result = await world.run("str_replace", path=FLAGGED, old_str="40", new_str="42")
    assert result.untrusted is True and EXTERNAL_REMINDER in result.text and OPEN in result.text
    assert "Kit A costs 42 USD." in result.text.split(OPEN)[1]
    assert result.text.startswith(f"replaced in workspace:{FLAGGED}")      # the code-written line stays outside


async def test_an_insert_echo_of_a_flagged_file_is_wrapped(world):
    result = await world.run("insert", path=FLAGGED, insert_line=-1, insert_text="Kit C costs 60 USD.")
    assert result.untrusted is True and OPEN in result.text
    assert "Kit C costs 60 USD." in result.text.split(OPEN)[1]


async def test_the_echo_of_an_unflagged_file_is_unchanged(world):
    replaced = await world.run("str_replace", path=PLAIN, old_str="40", new_str="42")
    inserted = await world.run("insert", path=PLAIN, insert_line=-1, insert_text="Kit C costs 60 USD.")
    for result in (replaced, inserted):
        assert result.untrusted is False and "external_content" not in result.text
    assert "Kit A costs 42 USD." in replaced.text


async def test_a_restore_and_history_of_a_flagged_file_print_no_bytes_and_keep_the_flag(world):
    await world.run("str_replace", path=FLAGGED, old_str="40", new_str="42")
    history = await world.run("history", path=FLAGGED)
    assert "Kit A" not in history.text and history.untrusted is False
    await world.run("view", path=FLAGGED)
    await world.run("delete", path=FLAGGED)
    first = min(e.generation for e in await world.store.list(USER, FLAGGED, include_versions=True))
    restored = await world.run("restore", path=FLAGGED, generation=str(first))
    assert "Kit A" not in restored.text and restored.untrusted is False
    assert [e.untrusted for e in await world.store.list(USER, FLAGGED)] == [MARK]       # restore keeps the flag
