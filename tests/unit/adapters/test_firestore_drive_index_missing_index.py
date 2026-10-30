"""A missing composite index is a store outage (final review F4-2; USER_DRIVE_INDEXING_RFC §8.2, §12.5): a query whose
index is missing or still building fails with FailedPrecondition, which leaves the repository as
DriveIndexStoreUnavailable — systemic for the tick, never a file's failure."""
import pytest
from google.api_core import exceptions as gexc
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery
from google.cloud.firestore_v1.async_query import AsyncQuery

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from src.domain.drive_index import DriveIndexStoreUnavailable
from tests.unit.services.drive_index_fakes import U_SCOPE


def _repo(mock_env_config):
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config)


def _failing(exc):
    async def fail(self, *args, **kwargs):
        raise exc
    return fail


async def test_a_query_without_its_index_is_a_store_outage(monkeypatch, mock_env_config):
    exc = gexc.FailedPrecondition("The query requires an index.")
    monkeypatch.setattr(AsyncQuery, "get", _failing(exc))
    with pytest.raises(DriveIndexStoreUnavailable) as raised:
        await _repo(mock_env_config).records_page(U_SCOPE, None, 10)
    assert raised.value.__cause__ is exc


async def test_the_chunk_total_without_its_index_is_a_store_outage(monkeypatch, mock_env_config):
    exc = gexc.FailedPrecondition("The query requires an index.")
    monkeypatch.setattr(AsyncAggregationQuery, "get", _failing(exc))
    with pytest.raises(DriveIndexStoreUnavailable):
        await _repo(mock_env_config).readable_chunk_total(U_SCOPE)
