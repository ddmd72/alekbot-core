"""The deleted-files search, and the Step 1 guard's line (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.8, §27.9.11).
Until the cut a `drive_deleted` flag on search_files_in_drive; since, DriveOperations.search_deleted (the specialist's
`search_deleted_recordings_and_notes` tool) beside the ordinary DriveOperations.search, and the guard on every
DriveOperations write (DRIVE_SPECIALIST_RFC §5.5, §8.1). A flag value the model sent is now the choice of a tool: the
former flag values are kept as cases of that choice."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_deleted_result import DeletedSearchResult
from src.domain.drive_search import STATUS_OK, DriveSearchResult
from src.domain.language import LanguageCode
from src.domain.user_drive import DriveMirrorWriteRefused
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


CTX = {"user_id": "u1", "account_id": "a1"}
WAIT = 100.0


@pytest.fixture
def search():
    svc = AsyncMock(spec=DriveSearchService)
    svc.search.return_value = DriveSearchResult(status=STATUS_OK, matched_total=0)
    svc.search_deleted.return_value = DeletedSearchResult("Deleted files (0):\nNo deleted file matched.", [])
    return svc


@pytest.fixture
def drive_service():
    return AsyncMock(spec=UserDriveService)


def _agent(search, drive_service, **extra):
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive_service,
                           drive_search=search, **extra)


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "Yes", "1", " true ", True])
async def test_drive_deleted_parsed_from_strings(search, drive_service, value):
    resp = await _agent(search, drive_service).search_deleted(CTX, "u1", "budget")
    assert resp.success
    search.search_deleted.assert_awaited_once_with("u1", "budget")
    search.search.assert_not_called()
    assert "Deleted files (0):" in resp.text


@pytest.mark.parametrize("value", ["false", "no", "0", "", "maybe", None, False])
async def test_anything_else_is_an_ordinary_search(search, drive_service, value):
    resp = await _agent(search, drive_service).search(CTX, "u1", query="budget")
    assert resp.success
    search.search.assert_awaited_once()
    search.search_deleted.assert_not_called()


async def test_an_empty_query_is_allowed_with_the_flag(search, drive_service):
    resp = await _agent(search, drive_service).search_deleted(CTX, "u1", "")
    assert resp.success
    search.search_deleted.assert_awaited_once_with("u1", "")


async def test_filters_are_not_required_and_not_read_with_the_flag(search, drive_service):
    await _agent(search, drive_service).search_deleted(CTX, "u1")   # the tool takes no filters at all
    search.search_deleted.assert_awaited_once()


async def test_the_result_carries_the_reference_reminder(search, drive_service):
    from src.domain.drive_search import REFERENCE_REMINDER
    resp = await _agent(search, drive_service).search_deleted(CTX, "u1")
    assert resp.text.startswith(REFERENCE_REMINDER)


async def test_no_search_service_says_so(drive_service):
    resp = await _agent(None, drive_service).search_deleted(CTX, "u1")
    assert not resp.success


# -- the Step 1 guard's line ----------------------------------------------------------------------------------

@pytest.mark.parametrize("op,args,kwargs", [
    ("save_text", ("x",), {"file_name": "a.md", "folder": "_Transcripts"}),
    ("delete", ("drive:f1",), {}),
    ("move", ("drive:f1",), {"folder": "Docs"}),
    ("create_folder", ("_Transcripts/x",), {}),
    ("append", ("drive:f1", "x"), {}),
], ids=["save_text", "delete", "move", "create_folder", "append"])
async def test_a_refused_write_answers_the_localized_line(search, drive_service, op, args, kwargs):
    for method in ("save", "ensure_folder", "move", "delete", "append_text", "replace_with"):
        getattr(drive_service, method).side_effect = DriveMirrorWriteRefused("mirror folder")
    localization = MagicMock()
    localization.get_ui_string.return_value = "Цю папку веде бот; змінюйте її в OneDrive."
    language = AsyncMock()
    language.resolve_ui_language.return_value = LanguageCode.UK
    agent = _agent(search, drive_service, localization=localization, language_service=language)
    resp = await getattr(agent, op)(CTX, "u1", *args, **kwargs, wait_s=WAIT)
    assert not resp.success
    assert resp.text == "Цю папку веде бот; змінюйте її в OneDrive."


async def test_a_refused_write_without_localization_falls_back_to_english(search, drive_service):
    drive_service.delete.side_effect = DriveMirrorWriteRefused("mirror folder")
    resp = await _agent(search, drive_service).delete(CTX, "u1", "drive:f1", wait_s=WAIT)
    assert not resp.success
    assert resp.text == ("This folder is managed by the bot and its files are overwritten; "
                          "to keep your own version, copy the file to another folder.")
    from src.domain.ui_messages import UIMessage
    from src.locales import en
    assert resp.text == en.UI_STRINGS[UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value]


def test_the_line_exists_in_every_language():
    from src.domain.ui_messages import UIMessage
    from src.locales import en, es, fr, uk
    for module in (en, es, fr, uk):
        strings = next(v for k, v in vars(module).items() if isinstance(v, dict) and UIMessage.DRIVE_DELETE_FAILED.value in v)
        assert strings[UIMessage.DRIVE_MIRROR_WRITE_REFUSED.value].strip()


async def test_shown_mirror_files_reach_drive_context(search, drive_service):
    from src.domain.user_drive import DriveItem
    item = DriveItem(item_id="m1", name="call1.md", path="_Transcripts/Archive/2026-10-05/call1.md", is_folder=False)
    search.search_deleted.return_value = DeletedSearchResult("Deleted files (1):\n...", [item])
    resp = await _agent(search, drive_service).search_deleted(CTX, "u1")
    assert resp.drive_context() == [
        {"path": "_Transcripts/Archive/2026-10-05/call1.md", "ref": "drive:m1"}]    # "open the second one" has a ref
