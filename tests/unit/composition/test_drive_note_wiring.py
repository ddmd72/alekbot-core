"""Wiring of drive notes on the main service (DRIVE_FILE_NOTES_RFC §3.6, §4.4, §11 S1): the kill switch (default on),
the service on the drive pair with its Firestore store, erase-request store, uncached users and the drive queue, and
nothing without the drive pair. (The agent that gets the service and the switch is the drive specialist, specialist RFC
§9.3 D1.)"""
import pytest

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.composition.drive_note_setup import drive_notes_switch_on
from src.config.environment import EnvironmentConfig
from src.domain.drive_note import ONEDRIVE_PROVIDER_KEY
from src.services.drive_note_service import DriveNoteService
from tests.unit.composition.test_drive_wiring import _container

_DRIVE = {"MICROSOFT_TODO_CLIENT_ID": "cid", "MICROSOFT_TODO_CLIENT_SECRET": "sec"}


@pytest.mark.parametrize("raw,on", [(None, True), ("", True), ("on", True), ("1", True), ("yes", True),
                                    ("0", False), ("off", False), ("false", False), (" No ", False)])
def test_the_switch_is_on_unless_explicitly_off(raw, on):
    assert drive_notes_switch_on(raw) is on


def test_no_service_without_the_drive_pair(monkeypatch):
    c = _container(monkeypatch)
    assert c.drive_note_service is None
    assert c.agent_services()["drive_note_service"] is None and c.agent_services()["drive_notes_enabled"] is True


def test_the_service_on_the_drive_pair(monkeypatch):
    monkeypatch.delenv("DRIVE_NOTES_ENABLED", raising=False)
    c = _container(monkeypatch, **_DRIVE)
    service = c.drive_note_service
    assert isinstance(service, DriveNoteService)
    assert isinstance(service._notes, FirestoreDriveNoteRepository)
    collections = [k.args[0] for k in service._notes._db.collection.call_args_list]
    assert EnvironmentConfig().drive_notes_collection in collections
    assert service._drive is c.user_drive and service._index is c.drive_index_repo
    assert service._nudge_queue is c.drive_task_queue
    assert service._embeddings is c.embedding_service
    assert service._space == GeminiEmbeddingAdapter.EMBEDDING_SPACE and service._provider == ONEDRIVE_PROVIDER_KEY
    assert isinstance(service._users, FirestoreUserRepository)
    assert isinstance(service._erase_requests, FirestoreEphemeralStore)
    assert service._erase_requests._collection == EnvironmentConfig().drive_note_erase_requests_collection
    assert c.agent_services()["drive_note_service"] is service


def test_switched_off_the_service_is_still_built_for_reads(monkeypatch):
    monkeypatch.setenv("DRIVE_NOTES_ENABLED", "off")
    c = _container(monkeypatch, **_DRIVE)
    assert c.drive_note_service is not None and c.drive_notes_enabled is False
    assert c.agent_services()["drive_notes_enabled"] is False

