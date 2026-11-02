"""G4 (GOOGLE_DRIVE_RFC): every printed path prints each name through `path_segment`. Synthetic records only."""
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_note_lifecycle import chain_path
from src.domain.drive_search import resolve_chain
from src.domain.drive_tool_texts import _FOLDER
from src.domain.user_drive import NAME_SLASH

GARDEN = "Garden plan 2026/27"
PRINTED = f"Garden plan 2026{NAME_SLASH}27"


def _rec(item_id, parent, name):
    return DriveIndexRecord("usr-1", item_id, parent_id=parent, name=name, state=RecordState.INDEXED,
                            scope=Scope.NORMAL)


def test_resolve_chain_prints_a_slash_inside_a_name_escaped():
    folder = _rec("f1", "root", GARDEN)
    plan = _rec("p1", "f1", "Beds.docx")
    path, ancestors = resolve_chain(plan, {"f1": folder}.get, "root", require_card=False)
    assert path == f"{PRINTED}/Beds.docx" and ancestors == ("f1",)


def test_chain_path_escapes_every_name():
    assert chain_path("a/b.txt", [GARDEN, "Clients: 2026"]) == f"Clients: 2026/{PRINTED}/a{NAME_SLASH}b.txt"


def test_chain_path_without_slashes_is_unchanged():
    assert chain_path("Beds.docx", ["Plans"]) == "Plans/Beds.docx"


def test_the_folder_argument_text_tells_to_copy_a_path_as_printed():
    assert f"copy a path as printed (a '/' inside a name prints as '{NAME_SLASH}')" in _FOLDER
