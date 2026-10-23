"""Facts S2 · T7: Cabinet invalidate over versions, through the real route, repository and adapter on the evaluating
Firestore fake. A stale id follows to the current version (200); an id that cannot be followed is a 409 with the
message and nothing written (not a 404: neither error is a ValueError, and the clause order keeps it so)."""
import copy
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.exceptions import FactResolutionError, FactWriteConflict
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config

_META = {"account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
         "context_priority": "medium", "tags": ["tea"], "metadata": {}, "type": "event"}


def _app(fact_repo):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_invalidate_versions")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=fact_repo, embedding_service=MagicMock(),
    ))
    return app


async def _invalidate(fact_repo, fact_id):
    async with _app(fact_repo).test_client() as client:
        resp = await client.post(f"/api/user/facts/{fact_id}/invalidate", headers={"Authorization": "Bearer t"})
        return resp.status_code, await resp.get_json()


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture
def repo(store):
    return FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder)


async def test_a_stale_id_invalidates_the_current_version(store, repo):
    old = (await store.adapter.create_fact("likes tea", _META))["fact_id"]
    new = (await store.adapter.update_fact(old, {"content": "likes black tea"}, ACC))["fact_id"]

    status, body = await _invalidate(repo, old)

    assert (status, body) == (200, {"status": "invalidated"})
    assert store.docs[new]["state"] == "invalidated"
    assert store.docs[old]["state"] == "superseded" and store.docs[old]["superseded_by"] == new


async def test_a_merged_source_is_a_409_with_the_message_and_nothing_written(store, repo):
    source = (await store.adapter.create_fact("likes tea", _META))["fact_id"]
    merged = (await store.adapter.merge_facts([source], "likes tea daily", _META))["new_fact_id"]
    before = copy.deepcopy(store.docs)

    status, body = await _invalidate(repo, source)

    assert status == 409
    assert "merged" in body["error"]
    assert store.docs == before and store.docs[merged]["state"] == "current"


async def test_an_unknown_id_is_still_a_404(repo):
    status, _ = await _invalidate(repo, "no-such-fact")

    assert status == 404


@pytest.mark.parametrize("error", [FactResolutionError("fact x was merged into y"), FactWriteConflict("x")])
async def test_both_errors_are_a_409_carrying_their_message(error):
    fact_repo = MagicMock(invalidate_fact=AsyncMock(side_effect=error), refresh_biographical_context_cache=AsyncMock())

    status, body = await _invalidate(fact_repo, "x")

    assert (status, body) == (409, {"error": str(error)})
    fact_repo.refresh_biographical_context_cache.assert_not_awaited()
