"""Owner-notes wiring (DRIVE_FILE_NOTES_RFC §3.6, §4.8, §6.4, §7): the kill switch is on unless switched off; with it
off the indexer gets no lifecycle; the erase requests live in their own collection; the deleted-files source joins the
search whatever the switch; the indexer passes the lifecycle to the tick."""
from unittest.mock import AsyncMock, MagicMock

import pytest

import indexer_main
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.composition.drive_note_setup import (
    add_drive_note_deleted_source, build_drive_note_erase_requests, build_drive_note_lifecycle, drive_notes_enabled,
)
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
from src.services.drive_note_lifecycle import DriveNoteLifecycle
from src.services.drive_search_service import DriveSearchService

from tests.unit.test_indexer_main import wired  # noqa: F401  (the indexer wiring fixture)


@pytest.mark.parametrize("value", [None, "", "on", "1", "true", "yes", "ON"])
def test_the_switch_is_on_by_default(value):
    assert drive_notes_enabled(value) is True


@pytest.mark.parametrize("value", ["0", "off", "false", "no", " Off "])
def test_the_switch_off(value):
    assert drive_notes_enabled(value) is False


def test_no_lifecycle_with_the_switch_off():
    assert build_drive_note_lifecycle(enabled=False, notes=MagicMock(), index=MagicMock(), drive=MagicMock()) is None
    assert isinstance(build_drive_note_lifecycle(enabled=True, notes=MagicMock(), index=MagicMock(),
                                                 drive=MagicMock()), DriveNoteLifecycle)


def test_the_erase_requests_have_their_own_collection():
    env = MagicMock()
    env.drive_note_erase_requests_collection = "development_drive_note_erase_requests"
    store = build_drive_note_erase_requests(MagicMock(), env)
    assert isinstance(store, FirestoreEphemeralStore) and store._collection == "development_drive_note_erase_requests"


def test_the_deleted_source_joins_the_search():
    search = DriveSearchService(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), embedding_space="s/8")
    source = add_drive_note_deleted_source(search, AsyncMock(), AsyncMock(), embedding_space="s/8")
    assert isinstance(source, DriveNoteDeletedSearch) and search._deleted_sources[-1] is source
    assert len(search._deleted_sources) == 2


async def test_the_indexer_passes_the_lifecycle_to_the_tick(wired, monkeypatch):  # noqa: F811
    monkeypatch.delenv("DRIVE_NOTES_ENABLED", raising=False)
    indexer_main.build_app()
    assert isinstance(wired["note_lifecycle"], DriveNoteLifecycle)


async def test_the_indexer_has_no_lifecycle_with_the_switch_off(wired, monkeypatch):  # noqa: F811
    monkeypatch.setenv("DRIVE_NOTES_ENABLED", "off")
    indexer_main.build_app()
    assert wired["note_lifecycle"] is None
