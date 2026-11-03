"""The caps table (EXTERNAL_FILES_RFC §4.5): every constant keeps today's value (D20 moved `MAX_CONVERTED_CHARS` to 60 000),
each lookup answers per store, and a skill or unknown store is refused. The names the table took over are importable only from it."""
import pytest

from src.domain import document_limits as limits
from src.domain.document_limits import (
    MAX_CONVERTED_CHARS, MAX_DRIVE_DOWNLOAD_BYTES, MAX_DRIVE_VISION_IMAGE_BYTES, MAX_DRIVE_VISION_PDF_BYTES,
    MAX_FILE_BYTES, MAX_TEXT_EXPORT_BYTES, MAX_VIDEO_ATTACH_BYTES, OPEN_MAX_CHARS, action_cap, native_ceiling,
    parse_input_cap, presentation_cap, reader_text_budget, text_export_cap,
)
from src.domain.file_ref import FileRefKind

MIB = 1024 * 1024
STORES = ["drive", "chat", "email", "workspace", "delivered"]


def test_every_constant_keeps_its_value():
    assert MAX_FILE_BYTES == 5 * MIB
    assert MAX_CONVERTED_CHARS == 60_000         # D20: one 60 000 cut on every open path
    assert MAX_TEXT_EXPORT_BYTES == 10 * MIB
    assert MAX_DRIVE_DOWNLOAD_BYTES == 45 * MIB
    assert MAX_DRIVE_VISION_IMAGE_BYTES == 5 * MIB
    assert MAX_DRIVE_VISION_PDF_BYTES == 20 * MIB
    assert OPEN_MAX_CHARS == 60_000
    assert MAX_VIDEO_ATTACH_BYTES == 45 * MIB


def test_the_alias_and_the_workspace_cap_stay_where_they_are():
    from src.domain import drive_index_params, workspace

    assert drive_index_params.TRANSCRIPT_OPEN_MAX_CHARS == OPEN_MAX_CHARS
    assert workspace.MAX_FILE_BYTES == 10 * MIB          # the workspace store's own cap, never the parse input cap


@pytest.mark.parametrize("store", STORES)
def test_parse_input_is_five_mib_in_every_store(store):
    assert parse_input_cap(store) == 5 * MIB


def test_text_export_and_action_caps():
    assert text_export_cap() == 10 * MIB
    assert action_cap() == 45 * MIB


@pytest.mark.parametrize("kind, cap", [("image", 5 * MIB), ("pdf", 20 * MIB), ("docx", 0), ("audio", 0), ("", 0)])
def test_native_ceiling_by_kind(kind, cap):
    assert native_ceiling(kind) == cap


@pytest.mark.parametrize("kind", ["image", "pdf", "docx", "video", "other"])
def test_native_ceiling_never_above_the_strictest_provider_limits(kind):
    assert native_ceiling(kind) <= {"image": 5 * MIB, "pdf": 20 * MIB}.get(kind, 0)


@pytest.mark.parametrize("store, cap", [   # D20: one 60 000 cut on every open path, email included
    ("drive", 60_000), ("email", 60_000), ("chat", 60_000), ("workspace", 60_000), ("delivered", 60_000),
])
def test_presentation_cap_by_store(store, cap):
    assert presentation_cap(store) == cap


@pytest.mark.parametrize("store, budget", [   # D7 follows the presentation cap; D20 makes it 60 000 everywhere
    ("drive", 60_000), ("email", 60_000), ("chat", 60_000), ("workspace", 60_000), ("delivered", 60_000),
])
def test_reader_text_budget_by_store(store, budget):
    assert reader_text_budget(store) == budget


def test_the_lookups_take_the_ref_kinds():
    assert presentation_cap(FileRefKind.DRIVE) == 60_000
    assert reader_text_budget(FileRefKind.CHAT) == 60_000
    assert parse_input_cap(FileRefKind.EMAIL) == 5 * MIB


@pytest.mark.parametrize("lookup", [parse_input_cap, presentation_cap, reader_text_budget])
@pytest.mark.parametrize("store", ["skill", FileRefKind.SKILL, "", "gmail", "DRIVE"])
def test_skill_and_unknown_stores_are_refused(lookup, store):
    with pytest.raises(ValueError):
        lookup(store)


@pytest.mark.parametrize("module, name", [
    ("src.utils.file_conversion", "MAX_FILE_BYTES"),
    ("src.utils.file_conversion", "MAX_CONVERTED_CHARS"),
    ("src.domain.text_data_uris", "MAX_TEXT_EXPORT_BYTES"),
    ("src.domain.user_drive", "MAX_DRIVE_DOWNLOAD_BYTES"),
    ("src.domain.user_drive", "MAX_DRIVE_VISION_IMAGE_BYTES"),
    ("src.domain.user_drive", "MAX_DRIVE_VISION_PDF_BYTES"),
    ("src.domain.drive_index_params", "OPEN_MAX_CHARS"),
    ("src.infrastructure.drive_operations", "MAX_VIDEO_ATTACH_BYTES"),
])
def test_each_definition_lives_in_the_table(module, name):
    import importlib

    old = importlib.import_module(module)
    assert getattr(old, name, getattr(limits, name)) is getattr(limits, name)   # absent, or the table's own object
