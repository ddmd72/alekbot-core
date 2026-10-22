"""Harness safety (DRIVE_SPECIALIST_RFC §12): in the drive evals no drive write ever runs. The specialist's tools reach
the drive only through DriveOperations, so the dry run stubs every mutation of the class — every instance, the
fixture's and the factory's — and refuses to install while a public method is unclassified. Lands before any dry run
of the specialist; the agent-level test at the end is the hard gate before any owner run (review I1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.validation.ab_agent_models import _READ_ONLY_INTENTS
from scripts.validation.drive_eval.dry_run_drive import (
    STUB_TEXT, DriveOpLog, DryRunDriveOperations, DryRunError, public_coroutines)
from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from src.composition.drive_specialist_build import build_drive_specialist
from src.domain import drive_tool_texts as tt
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.llm import PROMPT_CACHE_BOUNDARY
from src.domain.user import UserBotConfig
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.scripted_llm import calls, script

from .drive_eval_samples import MANIFEST

CONTEXT = {"user_id": "u1", "account_id": "a1"}
# method: (positional arguments after context and user_id, keywords, the logged arguments)
MUTATION_CALLS = {
    "copy": (("report.docx",), {"folder": "Inbox"}, {"source_ref": "report.docx", "folder": "Inbox"}),
    "save_text": (("milk, eggs",), {"file_name": "list.md"}, {"text": "milk, eggs", "file_name": "list.md"}),
    "create_folder": (("Archive/2026",), {}, {"folder": "Archive/2026"}),
    "move": (("drive:abc",), {"folder": "Archive"}, {"ref": "drive:abc", "folder": "Archive"}),
    "append": (("drive:abc", "one more line"), {}, {"ref": "drive:abc", "text": "one more line"}),
    "replace": (("drive:abc", "report.docx"), {}, {"ref": "drive:abc", "source_ref": "report.docx"}),
    "delete": (("drive:abc",), {}, {"ref": "drive:abc"}),
}
# UserDriveService methods that change the drive (resolve_folder creates folders when asked to)
SERVICE_WRITES = frozenset({"ensure_folder", "save", "move", "append_text", "replace_with", "delete",
                            "resolve_folder"})


def _ops(service) -> DriveOperations:
    conversion = MagicMock(spec=FileConversionService)
    conversion.resolve_bytes = AsyncMock(return_value=b"bytes")
    return DriveOperations(conversion_service=conversion, drive_service=service, drive_search=None)


def _writes(service) -> list:
    return [c for c in service.mock_calls if c[0].split(".")[0].split("(")[0] in SERVICE_WRITES]


def test_every_public_drive_operation_is_classified_read_or_mutation():
    reads, writes = set(DriveOperations.READ_METHODS), set(DriveOperations.MUTATION_METHODS)
    assert set(public_coroutines(DriveOperations)) == reads | writes and not reads & writes
    assert writes == set(MUTATION_CALLS)
    assert set(DriveOperations.OP_OF) == (reads | writes) - {"preflight", "folder_path"}


@pytest.mark.parametrize("method", sorted(MUTATION_CALLS))
async def test_a_mutation_under_the_dry_run_never_reaches_the_drive_service(method):
    service = AsyncMock(spec=UserDriveService)
    args, kwargs, logged = MUTATION_CALLS[method]
    log = DriveOpLog()
    with DryRunDriveOperations(log):
        out = await getattr(_ops(service), method)(CONTEXT, "u1", *args, **kwargs, wait_s=100.0)
    assert service.mock_calls == []
    assert out.cls is OutcomeClass.OK and out.success and out.text == STUB_TEXT.format(op=method)
    assert out.op is DriveOperations.OP_OF[method]
    assert log.entries == [{"op": method, "args": logged, "outcome": "ok", "stubbed": True}]


@pytest.mark.parametrize("method", sorted(MUTATION_CALLS))
async def test_the_same_mutation_without_the_dry_run_reaches_the_service(method):
    """Proves the test above can fail: the stub, not the arguments, is what keeps the service untouched."""
    service = AsyncMock(spec=UserDriveService)
    args, kwargs, _ = MUTATION_CALLS[method]
    await getattr(_ops(service), method)(CONTEXT, "u1", *args, **kwargs, wait_s=100.0)
    assert _writes(service) != []


async def test_a_read_runs_for_real_and_is_logged_with_its_outcome():
    drive = FixtureDrive(MANIFEST)
    log = DriveOpLog()
    ops = DriveOperations(conversion_service=FileConversionService(storage=MagicMock(), drive=drive),
                          drive_service=UserDriveService(drive), drive_search=None)
    with DryRunDriveOperations(log):
        out = await ops.list_folder(CONTEXT, "u1", "Contracts")
        state = await ops.preflight("u1")
    assert out.cls is OutcomeClass.OK and out.items and state is not None
    assert log.entries == [{"op": "list_folder", "args": {"folder": "Contracts"}, "outcome": "ok", "stubbed": False},
                           {"op": "preflight", "args": {}, "outcome": "ok", "stubbed": False}]


async def test_a_read_that_raises_is_logged_as_raised():
    class Failing(DriveOperations):
        async def list_folder(self, context, user_id, folder=None):
            raise RuntimeError("provider down")

    log = DriveOpLog()
    with DryRunDriveOperations(log, cls=Failing):
        with pytest.raises(RuntimeError):
            await Failing(conversion_service=MagicMock()).list_folder(CONTEXT, "u1", "Contracts")
    assert log.entries == [{"op": "list_folder", "args": {"folder": "Contracts"}, "outcome": "raised",
                            "stubbed": False}]


async def test_leaving_the_dry_run_restores_the_class():
    originals = {name: getattr(DriveOperations, name)
                 for name in DriveOperations.MUTATION_METHODS | DriveOperations.READ_METHODS}
    with DryRunDriveOperations(DriveOpLog()):
        assert all(getattr(DriveOperations, n) is not f for n, f in originals.items())
    assert all(getattr(DriveOperations, n) is f for n, f in originals.items())


def test_an_unclassified_public_operation_refuses_the_install():
    class Wider(DriveOperations):
        async def rename_everything(self, context, user_id):   # a new write nobody classified
            return None

    with pytest.raises(DryRunError, match="rename_everything"):
        DryRunDriveOperations(DriveOpLog(), cls=Wider).install()


def test_the_model_ab_dry_run_never_runs_the_drive():
    """ab_agent_models.py stubs every intent outside its read-only set at Smart's dispatch, as it stubbed the 8 drive
    intents: the drive specialist is never reached there, so it needs no DriveOperations stub (RFC delta, §12)."""
    assert Intent.WORK_WITH_DRIVE_FILES not in _READ_ONLY_INTENTS


def _context_builder() -> MagicMock:
    builder = MagicMock()
    builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    return builder


async def test_the_fixtures_specialist_and_file_management_share_one_operations_object_and_no_notifier():
    """Whatever the specialist's tools call, they call `agent.operations`: an instance of the patched class, built
    with no notification service, so neither a receipt nor a video can reach the owner's chat."""
    from scripts.validation.drive_eval.specialist_fixture import build_drive_fixture

    fixture = build_drive_fixture("u1", FixtureDrive(MANIFEST), MagicMock(), user_config=UserBotConfig(),
                                  prompt_builder=MagicMock(), context_builder=_context_builder())
    assert fixture.drive_agent.operations is fixture.file_management.drive_operations is fixture.operations
    assert type(fixture.operations) is DriveOperations
    assert fixture.drive_agent.agent_id == "drive_agent_u1"
    assert fixture.coordinator.get_agent("drive_agent_u1") is fixture.drive_agent


async def test_the_fixtures_video_open_cannot_reach_a_chat():
    """No notifier, observed through behaviour: a delivered video opened from storage is refused, not sent."""
    from scripts.validation.drive_eval.specialist_fixture import build_drive_fixture

    fixture = build_drive_fixture("u1", FixtureDrive(MANIFEST), MagicMock())
    fixture.operations._conversion_service.resolve_bytes = AsyncMock(return_value=b"v" * 10)
    out = await fixture.operations.fetch_stored(CONTEXT, "u1", "video_generation/clip.mp4")
    assert not out.success and "not configured" in out.text


async def test_the_eval_user_search_asks_every_method_as_the_eval_user():
    from scripts.validation.drive_eval.runner import EVAL_USER
    from scripts.validation.drive_eval.specialist_fixture import EvalUserSearch
    from src.domain.drive_search_line import SearchLine

    real = MagicMock()
    real.search_deleted = AsyncMock(return_value=("Deleted files (0):", []))
    real.search_state = AsyncMock(return_value=SearchLine.ON)
    as_user = EvalUserSearch(real)
    assert await as_user.search_deleted("owner", "garden") == ("Deleted files (0):", [])
    real.search_deleted.assert_awaited_once_with(EVAL_USER, "garden")
    assert await as_user.search_state("owner") is SearchLine.ON
    real.search_state.assert_awaited_once_with(EVAL_USER)


async def test_a_drive_off_case_says_off_in_the_state_block_too():
    """I2: production reads one flag for the search and the state block's `search` line, so the eval does too."""
    from scripts.validation.drive_eval.specialist_fixture import EvalUserSearch
    from src.domain.drive_search import DriveSearchRequest, STATUS_DISABLED
    from src.domain.drive_search_line import SearchLine

    real = MagicMock()
    real.search_state = AsyncMock(return_value=SearchLine.ON)
    as_user = EvalUserSearch(real)
    as_user.off = True
    assert await as_user.search_state("owner") is SearchLine.OFF
    assert (await as_user.search("owner", DriveSearchRequest(query="lease"))).status == STATUS_DISABLED
    real.search_state.assert_not_called()


async def test_every_write_tool_of_the_specialist_is_stubbed_in_the_dry_run():
    """The agent-level proof (RFC §11.1 "Harness", review I1): the real DriveAgent, driven by a scripted LLM through
    every write tool, reaches only DriveOperations' class methods — every mutation is stubbed and the drive service
    sees no write — while the reads it needs run for real: the preflight and the `drive:` folder ref's lookup."""
    service = AsyncMock(spec=UserDriveService)
    service.connection.return_value = DriveConnection(connected=True)
    service.get_item.return_value = DriveItem(item_id="fold1", name="Archive", path="Archive", is_folder=True)
    ops = _ops(service)
    prompts = AsyncMock()
    prompts.build_for_agent.return_value = f"DriveAgent extends Agent {{}}\n\n{PROMPT_CACHE_BOUNDARY}"
    log = DriveOpLog()
    with DryRunDriveOperations(log):
        agent = build_drive_specialist("u1", context_builder=_context_builder(), prompt_builder=prompts,
                                       user_config=UserBotConfig(), operations=ops, alert_sink=None)
        agent.coordinator = MagicMock()
        agent._call_llm, _ = script(
            calls((tt.COPY_FILE_TO_DRIVE, {"source_ref": "report.docx"}),
                  (tt.SAVE_TEXT_FILE, {"text_item": "t1"}),
                  (tt.CREATE_FOLDER, {"folder": "Archive/2026"}),
                  (tt.MOVE_FILE, {"file_ref": "drive:abc", "folder": "drive:fold1"}),
                  (tt.APPEND_TO_FILE, {"text_item": "t1", "file_ref": "drive:abc"}),
                  (tt.REPLACE_FILE_CONTENT, {"target_ref": "drive:abc", "new_content_ref": "report.docx"}),
                  (tt.DELETE_DRIVE_ITEM, {"file_ref": "drive:def"})),
            calls((tt.REPORT_TOOL, {"note": "Did the seven changes."})))
        message = AgentMessage.create(
            sender="coordinator", recipient="drive_agent_u1", intent=AgentIntent.QUERY,
            payload={"query": "do every change", "file_contents": [{"name": "n.md", "text": "x"}]},
            context=dict(CONTEXT))
        resp = await agent.execute(message)
    assert _writes(service) == []
    assert sorted(e["op"] for e in log.entries if e["stubbed"]) == sorted(DriveOperations.MUTATION_METHODS)
    assert {"op": "preflight", "args": {}, "outcome": "ok", "stubbed": False} in log.entries
    assert {"op": "folder_path", "args": {"folder": "drive:fold1"}, "outcome": "ok", "stubbed": False} in log.entries
    service.get_item.assert_awaited_once_with("u1", "fold1")      # the real folder_path read ran
    moved = next(e for e in log.entries if e["op"] == "move")
    assert moved["args"] == {"ref": "drive:abc", "folder": "Archive", "new_name": None}
    assert resp.result.splitlines()[0] == "Drive result: ok"
