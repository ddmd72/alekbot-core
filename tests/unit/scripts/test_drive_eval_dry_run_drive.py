"""Harness safety (DRIVE_SPECIALIST_RFC §12): in the drive evals no drive write ever runs. The specialist's tools reach
the drive only through DriveOperations, so the dry run stubs every mutation of the class — every instance, the
fixture's and the factory's — and refuses to install while a public method is unclassified. Lands before any dry run
of the specialist."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.validation.ab_agent_models import _READ_ONLY_INTENTS
from scripts.validation.drive_eval.dry_run_drive import (
    STUB_TEXT, DriveOpLog, DryRunDriveOperations, DryRunError, public_coroutines)
from scripts.validation.drive_eval.fixture_drive import FixtureDrive
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.drive_op import DriveOutcome
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

from .drive_eval_samples import MANIFEST

MUTATION_CALLS = {
    "copy": {"source_ref": "report.docx", "folder": "Inbox"},
    "save_text": {"text": "milk, eggs", "file_name": "list.md"},
    "create_folder": {"folder": "Archive/2026"},
    "move": {"file_ref": "drive:abc", "folder": "Archive"},
    "append": {"file_ref": "drive:abc", "text": "one more line"},
    "replace": {"target_ref": "drive:abc", "source_ref": "report.docx"},
    "delete": {"file_ref": "drive:abc"},
}


def _message() -> AgentMessage:
    return AgentMessage.create(sender="coordinator", recipient="drive_agent_u1", intent=AgentIntent.QUERY,
                               payload={}, context={"user_id": "u1", "account_id": "a1"})


def _ops(service) -> DriveOperations:
    conversion = MagicMock(spec=FileConversionService)
    conversion.resolve_bytes = AsyncMock(return_value=b"bytes")
    return DriveOperations(conversion_service=conversion, drive_service=service, drive_search=None)


def test_every_public_drive_operation_is_classified_read_or_mutation():
    reads, writes = set(DriveOperations.READ_METHODS), set(DriveOperations.MUTATION_METHODS)
    assert set(public_coroutines(DriveOperations)) == reads | writes and not reads & writes
    assert writes == set(MUTATION_CALLS)
    assert set(DriveOperations.OP_OF) == reads | writes


@pytest.mark.parametrize("method", sorted(MUTATION_CALLS))
async def test_a_mutation_under_the_dry_run_never_reaches_the_drive_service(method):
    service = AsyncMock(spec=UserDriveService)
    log = DriveOpLog()
    with DryRunDriveOperations(log):
        out = await getattr(_ops(service), method)(_message(), **MUTATION_CALLS[method], wait_s=100.0)
    assert service.mock_calls == []
    assert out.outcome is DriveOutcome.OK and out.text == STUB_TEXT.format(op=method)
    assert log.entries == [{"op": method, "args": MUTATION_CALLS[method], "outcome": "ok", "stubbed": True,
                            "agent": "drive_agent_u1"}]


@pytest.mark.parametrize("method", sorted(MUTATION_CALLS))
async def test_the_same_mutation_without_the_dry_run_reaches_the_service(method):
    """Proves the test above can fail: the stub, not the arguments, is what keeps the service untouched."""
    service = AsyncMock(spec=UserDriveService)
    try:
        await getattr(_ops(service), method)(_message(), **MUTATION_CALLS[method], wait_s=100.0)
    except Exception:   # noqa: BLE001 — a mock's return shape may not satisfy the operation; the call is what counts
        pass
    assert service.mock_calls != []


async def test_a_read_runs_for_real_and_is_logged_with_its_outcome():
    drive = FixtureDrive(MANIFEST)
    log = DriveOpLog()
    ops = DriveOperations(conversion_service=FileConversionService(storage=MagicMock(), drive=drive),
                          drive_service=UserDriveService(drive), drive_search=None)
    with DryRunDriveOperations(log):
        out = await ops.list_folder(_message(), folder="Contracts")
    assert out.outcome is DriveOutcome.OK and out.items
    assert log.entries == [{"op": "list_folder", "args": {"folder": "Contracts"}, "outcome": "ok", "stubbed": False,
                            "agent": "drive_agent_u1"}]


async def test_leaving_the_dry_run_restores_the_class():
    originals = {name: getattr(DriveOperations, name) for name in DriveOperations.MUTATION_METHODS}
    with DryRunDriveOperations(DriveOpLog()):
        assert all(getattr(DriveOperations, n) is not f for n, f in originals.items())
    assert all(getattr(DriveOperations, n) is f for n, f in originals.items())


def test_an_unclassified_public_operation_refuses_the_install():
    class Wider(DriveOperations):
        async def rename_everything(self, message, **kwargs):   # a new write nobody classified
            return None

    with pytest.raises(DryRunError, match="rename_everything"):
        DryRunDriveOperations(DriveOpLog(), cls=Wider).install()


def test_the_model_ab_dry_run_never_runs_the_drive():
    """ab_agent_models.py stubs every intent outside its read-only set at Smart's dispatch, as it stubbed the 8 drive
    intents: the drive specialist is never reached there, so it needs no DriveOperations stub (RFC delta, §12)."""
    assert Intent.WORK_WITH_DRIVE_FILES not in _READ_ONLY_INTENTS


async def test_the_fixtures_specialist_and_file_management_share_one_operations_object_and_no_notifier():
    """Whatever the specialist's tools call, they call `agent.operations`: an instance of the patched class, built
    with no notification service, so neither a receipt nor a video can reach the owner's chat."""
    from scripts.validation.drive_eval.specialist_fixture import build_drive_fixture
    from src.domain.user import UserBotConfig

    context_builder = MagicMock()
    context_builder.build.return_value = MagicMock(provider=MagicMock(), model_name="claude-sonnet-5-5")
    fixture = build_drive_fixture("u1", FixtureDrive(MANIFEST), MagicMock(), user_config=UserBotConfig(),
                                  prompt_builder=MagicMock(), context_builder=context_builder)
    assert fixture.drive_agent.operations is fixture.file_management.drive_operations is fixture.operations
    assert type(fixture.operations) is DriveOperations and fixture.operations.notification is None
    assert fixture.drive_agent.agent_id == "drive_agent_u1"
    assert fixture.coordinator.get_agent("drive_agent_u1") is fixture.drive_agent


async def test_the_eval_user_search_asks_every_method_as_the_eval_user():
    from scripts.validation.drive_eval.runner import EVAL_USER
    from scripts.validation.drive_eval.specialist_fixture import EvalUserSearch

    real = MagicMock()
    real.search_deleted = AsyncMock(return_value=("Deleted files (0):", []))
    real.search_state = AsyncMock(return_value="on")
    as_user = EvalUserSearch(real)
    assert await as_user.search_deleted("owner", "garden") == ("Deleted files (0):", [])
    real.search_deleted.assert_awaited_once_with(EVAL_USER, "garden")
    assert await as_user.search_state("owner") == "on"
    real.search_state.assert_awaited_once_with(EVAL_USER)
