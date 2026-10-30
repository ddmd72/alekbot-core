"""Cabinet delete of a row whose fact was rewritten after it was shown (owner, 2026-10-23), through the real route,
repository and adapter on the evaluating Firestore fake: a 409 the page answers with a reload, nothing written, and the
new text stays current."""
import copy
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.adapters.firestore_repo import FirestoreFactRepository
from src.services.cabinet_fact_service import CabinetFactService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config
from tests.support.fact_write import EXPIRY_NONE, ctx
from src.composition.connector_registry import ConnectorRegistry

_META = {"account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
         "context_priority": "medium", "tags": ["tea"], "metadata": {}, "type": "event", "expiry": EXPIRY_NONE}


def _app(fact_repo):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_invalidate_rewritten")
    # Reviewer edit (facts S4, T2c): the route delegates to CabinetFactService, which the blueprint now takes.
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=fact_repo, embedding_service=MagicMock(), cabinet_facts=CabinetFactService(fact_repo), connectors=ConnectorRegistry(),
    ))
    return app


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def test_a_row_whose_text_was_rewritten_is_a_409_and_nothing_is_written(store):
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder)
    shown = (await store.adapter.create_fact("likes tea", _META, context=ctx()))["fact_id"]
    rewritten = (await store.adapter.update_fact(
        shown, {"content": "likes green tea", "expiry": EXPIRY_NONE}, ACC, context=ctx()))["fact_id"]
    before = copy.deepcopy(store.docs)

    async with _app(repo).test_client() as client:
        resp = await client.post(f"/api/user/facts/{shown}/invalidate", headers={"Authorization": "Bearer t"})
        status, body = resp.status_code, await resp.get_json()

    assert status == 409
    assert "rewritten after it was shown" in body["error"]
    assert store.docs == before
    assert store.docs[rewritten]["state"] == "current" and store.docs[rewritten]["text"] == "likes green tea"
