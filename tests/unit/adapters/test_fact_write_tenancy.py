"""Tenancy of an in-place write is the stored `account_id` (S4 T2c review, H1): `update_fact_fields` refuses with
PermissionError inside its transaction, and writes nothing, when the stored document is not the account `stored` was read
as. By-id reads show a legacy `owner_id` as the account, so without this check the Cabinet delete reached a pre-migration
fact and a hybrid one (another account's `account_id` beside a stray `owner_id` of the caller).

Real repository and CabinetFactService on the evaluating Firestore fake; the dry-run kit's overlay store for the same rule."""
import copy

import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactState
from src.domain.fact_lifecycle import CABINET_ACCEPTS
from src.domain.stored_fact import StoredFact
from src.services.cabinet_fact_service import CabinetFactService
from tests.support.fact_kit import ACC, DROP, OTHER, as_legacy, make_fact, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore

INVALIDATE = {"state": FactState.INVALIDATED, "is_current": True}


@pytest.fixture
def store():
    store = FactStore(indexes=INDEXES_FILE)
    put_raw(store, "hybrid", "hybrid", account_id=OTHER, owner_id=ACC, created_by_user_id=DROP)
    put_raw(store, "legacy", "legacy", **as_legacy())
    put_raw(store, "legacy-other", "legacy other", **as_legacy(owner_id=OTHER))
    put_raw(store, "mine", "mine")
    return store


@pytest.mark.parametrize("fact_id", ["hybrid", "legacy", "legacy-other"])
async def test_the_cabinet_delete_refuses_a_document_not_stored_as_the_callers_and_writes_nothing(store, fact_id):
    before = copy.deepcopy(store.docs)

    with pytest.raises(PermissionError):
        await CabinetFactService(store.repo).invalidate(fact_id, ACC)

    assert store.docs == before


@pytest.mark.parametrize("fact_id", ["hybrid", "legacy"])
async def test_update_fact_fields_refuses_a_document_the_read_shows_as_the_callers(store, fact_id):
    stored = await store.repo.resolve_current(fact_id, ACC, CABINET_ACCEPTS)
    assert stored is not None and stored.fact.account_id == ACC        # the read's view: the owner_id compatibility mapping
    before = copy.deepcopy(store.docs)

    with pytest.raises(PermissionError, match="Not authorized to modify this fact"):
        await store.repo.update_fact_fields(stored, INVALIDATE)

    assert store.docs == before


async def test_update_fact_fields_refuses_a_stored_fact_read_as_another_account(store):
    mine = await store.repo.resolve_current("mine", ACC, CABINET_ACCEPTS)
    as_other = StoredFact(mine.fact.model_copy(update={"account_id": OTHER}), mine.update_time)
    before = copy.deepcopy(store.docs)

    with pytest.raises(PermissionError):
        await store.repo.update_fact_fields(as_other, INVALIDATE)

    assert store.docs == before


async def test_a_document_stored_as_the_callers_is_still_written(store):
    stored = await store.repo.resolve_current("mine", ACC, CABINET_ACCEPTS)

    await store.repo.update_fact_fields(stored, INVALIDATE)

    assert store.docs["mine"]["state"] == "invalidated" and store.docs["mine"]["account_id"] == ACC


async def test_the_cabinet_delete_of_the_callers_own_fact_still_succeeds(store):
    await CabinetFactService(store.repo).invalidate("mine", ACC)

    assert store.docs["mine"]["state"] == "invalidated"


async def test_the_kit_refuses_a_stored_fact_read_as_another_account_and_keeps_its_overlay(store):
    kit = NoWriteFactRepository(store.repo)
    await kit.add_fact(make_fact("k"))
    stored = await kit.resolve_current("f-k", ACC, CABINET_ACCEPTS)
    as_other = StoredFact(stored.fact.model_copy(update={"account_id": OTHER}), stored.update_time)

    with pytest.raises(PermissionError, match="Not authorized to modify this fact"):
        await kit.update_fact_fields(as_other, INVALIDATE)

    assert kit.overlay_document("f-k").state is FactState.CURRENT
    assert [w["kind"] for w in kit.writes] == ["add_fact"]

    await kit.update_fact_fields(stored, INVALIDATE)
    assert kit.overlay_document("f-k").state is FactState.INVALIDATED
