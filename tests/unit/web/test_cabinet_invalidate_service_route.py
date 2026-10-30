"""The Cabinet delete route over CabinetFactService (S4 T2c): the real route, service and repository on the evaluating
Firestore fake. Each outcome keeps today's HTTP code and body; without the service the route answers 503."""
import copy
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.domain.entities import FactState
from src.services.cabinet_fact_service import CabinetFactService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, OTHER, T0, USER, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore
from src.composition.connector_registry import ConnectorRegistry


def _app(store, cabinet_facts):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_invalidate_service_route")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=store.repo, embedding_service=MagicMock(), cabinet_facts=cabinet_facts, connectors=ConnectorRegistry(),
    ))
    return app


async def _post(store, fact_id, cabinet_facts):
    async with _app(store, cabinet_facts).test_client() as client:
        resp = await client.post(f"/api/user/facts/{fact_id}/invalidate", headers={"Authorization": "Bearer t"})
        return resp.status_code, await resp.get_json()


class Exploding(CabinetFactService):
    async def invalidate(self, fact_id, account_id):
        raise RuntimeError("store down")


@pytest.fixture
async def store():
    store = FactStore(indexes=INDEXES_FILE)
    for fact in (
        make_fact("x"),
        make_fact("foreign", account=OTHER),
        make_fact("v1", lineage_id="L", state=FactState.SUPERSEDED, is_current=False, valid_to=T0, superseded_by="f-v2"),
        make_fact("v2", lineage_id="L", version=2),        # another text than f-v1: the page never showed it
    ):
        await store.repo.add_fact(fact)
    return store


async def test_an_owned_fact_is_invalidated_and_answered_200(store):
    status, body = await _post(store, "f-x", CabinetFactService(store.repo))

    assert (status, body) == (200, {"status": "invalidated"})
    assert store.docs["f-x"]["state"] == "invalidated"


@pytest.mark.parametrize("fact_id, want", [
    ("f-foreign", (403, {"error": "Not authorized"})),
    ("f-missing", (404, {"error": "Fact not found: f-missing"})),
    ("f-v1", (409, {"error": "fact f-v1 was rewritten after it was shown; reload"})),
])
async def test_each_refusal_keeps_its_code_and_body_and_writes_nothing(store, fact_id, want):
    before = copy.deepcopy(store.docs)

    assert await _post(store, fact_id, CabinetFactService(store.repo)) == want
    assert store.docs == before


async def test_an_unexpected_error_is_a_500(store):
    assert await _post(store, "f-x", Exploding(store.repo)) == (500, {"error": "Internal server error"})


async def test_without_the_service_the_route_answers_503_and_writes_nothing(store):
    before = copy.deepcopy(store.docs)

    assert await _post(store, "f-x", None) == (503, {"error": "unavailable"})
    assert store.docs == before
