"""The provider account of a connection in the Firestore store and its in-memory twin: stored as realm and subject,
read back with an empty label, set by `update`, None for a document written before the field."""
import pytest

from src.domain.provider_account import ProviderAccount
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

from tests.unit.adapters.test_drive_connection_store_kit import CONNS, conn, firestore_repo

ACCOUNT = ProviderAccount(realm="realm-x", subject="sub-1", label="")


@pytest.fixture(params=["firestore", "fake"])
def repo(request):
    return firestore_repo()[0] if request.param == "firestore" else FakeDriveConnectionRepository()


async def test_the_account_round_trips_through_create(repo):
    await repo.create(conn("usr-1", "conn-1", provider_account=ACCOUNT))
    assert (await repo.get("usr-1", "conn-1")).provider_account == ACCOUNT


async def test_update_sets_the_account_and_keeps_the_other_fields(repo):
    await repo.create(conn("usr-1", "conn-1"))
    assert (await repo.get("usr-1", "conn-1")).provider_account is None
    updated = await repo.update("usr-1", "conn-1", provider_account=ACCOUNT)
    assert updated.provider_account == ACCOUNT and updated.credential_id == "cred-conn-1"
    assert (await repo.get("usr-1", "conn-1")).provider_account == ACCOUNT


async def test_a_document_without_the_fields_reads_as_no_account():
    repo, db = firestore_repo()
    await repo.create(conn("usr-1", "conn-1", provider_account=ACCOUNT))
    ref = db.collection(CONNS).document("usr-1:conn-1")
    snap = await ref.get()
    data = {k: v for k, v in snap.to_dict().items() if not k.startswith("provider_")}
    await ref.set(data)
    assert (await repo.get("usr-1", "conn-1")).provider_account is None
