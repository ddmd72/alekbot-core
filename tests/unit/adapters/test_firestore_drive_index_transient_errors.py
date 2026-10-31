"""Transient Firestore errors leave the drive-index repository as one domain error (USER_DRIVE_INDEXING_RFC §8.2;
round-2 review T-m4): DeadlineExceeded, ServiceUnavailable, InternalServerError, Aborted and ResourceExhausted become
DriveIndexStoreUnavailable, which the tick treats as systemic. Any other error keeps its own type."""
import pytest
from google.api_core import exceptions as gexc
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_batch import AsyncWriteBatch
from google.cloud.firestore_v1.async_query import AsyncQuery

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveIndexStoreUnavailable
from tests.unit.services.drive_index_fakes import U_SCOPE

TRANSIENT = [gexc.DeadlineExceeded("d"), gexc.ServiceUnavailable("s"), gexc.InternalServerError("i"),
             gexc.Aborted("a"), gexc.ResourceExhausted("r")]


def _repo(mock_env_config):
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config)


def _failing(exc):
    async def fail(self, *args, **kwargs):
        raise exc
    return fail


@pytest.mark.parametrize("exc", TRANSIENT, ids=lambda e: type(e).__name__)
async def test_a_transient_write_error_is_a_store_outage(monkeypatch, mock_env_config, exc):
    monkeypatch.setattr(AsyncWriteBatch, "commit", _failing(exc))
    with pytest.raises(DriveIndexStoreUnavailable) as raised:
        await _repo(mock_env_config).upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="i")])
    assert raised.value.__cause__ is exc


@pytest.mark.parametrize("exc", TRANSIENT, ids=lambda e: type(e).__name__)
async def test_a_transient_read_error_is_a_store_outage(monkeypatch, mock_env_config, exc):
    monkeypatch.setattr(AsyncQuery, "get", _failing(exc))
    with pytest.raises(DriveIndexStoreUnavailable):
        await _repo(mock_env_config).records_page(U_SCOPE, None, 10)


async def test_another_error_keeps_its_type(monkeypatch, mock_env_config):
    monkeypatch.setattr(AsyncQuery, "get", _failing(gexc.PermissionDenied("no")))
    with pytest.raises(gexc.PermissionDenied):
        await _repo(mock_env_config).records_page(U_SCOPE, None, 10)
