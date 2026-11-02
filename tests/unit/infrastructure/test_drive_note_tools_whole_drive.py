"""The note tools take exactly one target, `item_ref` or `whole_drive_name` (MULTI_DRIVE_RFC §11.10, T-13 tool half;
slice 2b P-D): declarations, the exactly-one refusals, the service call per target, a folder note by item_ref, the
rename notice, the read-first key for a drive, and the owner-turn gate. Synthetic data only."""
import pytest

from src.domain import drive_note_tool_texts as nt
from src.domain.drive_mounts import RENAME_NOTICE
from src.domain.drive_outcome_class import OutcomeClass
from tests.unit.infrastructure.test_drive_note_tools_specialist import (
    OWNER, REPAIR, _call, _receipt, _service, _tools, _view,
)

ACME = "Acme work files: R&D, Tom's plan"


def _declared(writes=True):
    return {d["name"]: d for d in nt.drive_note_tool_declarations(writes=writes)}


@pytest.mark.parametrize("name", nt.NOTE_TOOLS)
def test_every_tool_declares_whole_drive_name_and_item_ref_is_not_required(name):
    params = _declared()[name]["parameters"]
    assert params["properties"]["whole_drive_name"]["type"] == "string"
    assert "item_ref" not in params["required"] and "whole_drive_name" not in params["required"]
    assert "Only for a note about the entire drive" in params["properties"]["whole_drive_name"]["description"]
    assert "item_ref" in params["properties"]


@pytest.mark.parametrize("name,args", [
    (nt.GET_NOTE, {}), (nt.CREATE_NOTE, {"text": ACME}), (nt.ADD_TO_NOTE, {"text": ACME}),
    (nt.DELETE_NOTE, {}), (nt.UNDO_NOTE, {}), (nt.ERASE_NOTE, {})])
@pytest.mark.parametrize("given", ["neither", "both"])
async def test_neither_or_both_targets_are_refused_before_any_service_call(name, args, given):
    service = _service()
    notes, _, collector = _tools(service)
    extra = {"item_ref": "drive:a1:a", "whole_drive_name": "Acme"} if given == "both" else {}

    await _call(notes, name, **args, **extra)

    assert service.mock_calls == []
    outcome = collector.results[0].outcome
    assert outcome.cls is OutcomeClass.REQUEST_ERROR and outcome.text == nt.ONE_TARGET


async def test_a_whole_drive_write_passes_the_name_and_an_empty_ref():
    service = _service()
    notes, _, _ = _tools(service)

    await _call(notes, nt.CREATE_NOTE, whole_drive_name="  Acme ", text=ACME)

    args, kwargs = service.create.await_args
    assert args[2] == "" and kwargs == {"whole_drive_name": "Acme"}


@pytest.mark.parametrize("name,method", [(nt.DELETE_NOTE, "clear"), (nt.UNDO_NOTE, "undo"),
                                         (nt.ERASE_NOTE, "request_erase"), (nt.ADD_TO_NOTE, "update")])
async def test_each_writer_forwards_the_drive_name(name, method):
    service = _service()
    notes, _, _ = _tools(service)

    await _call(notes, name, whole_drive_name="Acme", text=ACME)

    assert getattr(service, method).await_args.kwargs["whole_drive_name"] == "Acme"


async def test_a_folder_note_by_item_ref_never_carries_a_drive_name():
    service = _service()
    notes, _, _ = _tools(service)

    await _call(notes, nt.CREATE_NOTE, item_ref="drive:a1:rd", text="R&D")

    args, kwargs = service.create.await_args
    assert args[2] == "drive:a1:rd" and "whole_drive_name" not in kwargs


async def test_get_note_by_drive_name_reads_through_the_service():
    service = _service()
    service.get.return_value = _view(text=ACME)
    notes, _, collector = _tools(service)

    await _call(notes, nt.GET_NOTE, whole_drive_name="Acme")

    assert service.get.await_args.kwargs == {"whole_drive_name": "Acme"}
    assert ACME in collector.results[0].outcome.text


async def test_replace_after_a_read_of_the_same_drive_uses_the_read_version_and_without_one_is_refused():
    service = _service()
    service.get.return_value = _view(version=4)
    notes, _, collector = _tools(service)

    await _call(notes, nt.REPLACE_NOTE_TEXT, whole_drive_name="Acme", text=REPAIR)
    assert service.update.await_count == 0 and collector.results[0].outcome.text == nt.READ_FIRST

    await _call(notes, nt.GET_NOTE, whole_drive_name="acme")
    await _call(notes, nt.REPLACE_NOTE_TEXT, whole_drive_name="Acme", text=REPAIR)
    assert service.update.await_args.kwargs["expected_version"] == 4


async def test_a_former_name_puts_the_rename_notice_first_in_the_result():
    service = _service()
    service.create.return_value = _receipt(ref="")
    notice = RENAME_NOTICE.format(former="Acme", current="Acme Hamburg")
    service.drive_rename_notice.return_value = notice
    notes, _, collector = _tools(service)

    await _call(notes, nt.CREATE_NOTE, whole_drive_name="Acme", text=ACME)

    assert collector.results[0].outcome.text.startswith(notice + "\n")


async def test_no_writer_is_declared_or_served_off_the_owner_turn():
    service = _service()
    notes, _, collector = _tools(service, context={"user_id": "usr-1"}, writes=False)

    assert list(notes.handlers) == [nt.GET_NOTE]
    assert [d["name"] for d in nt.drive_note_tool_declarations(writes=False)] == [nt.GET_NOTE]


async def test_a_writer_called_off_the_owner_turn_is_refused_whatever_the_target():
    service = _service()
    notes, _, collector = _tools(service, context={"user_id": "usr-1"}, writes=True)

    await _call(notes, nt.CREATE_NOTE, whole_drive_name="Acme", text=ACME)

    assert service.mock_calls == [] and collector.results[0].outcome.text == nt.NOT_OWNER_TURN
