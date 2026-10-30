"""Cabinet invalidate removes the fact from the baseline (the user_context cache) at once, not at the next consolidation.

Real FirestoreFactRepository + BiographicalContextService on the evaluating Firestore fake; the Quart route is exercised
through its test client."""
import logging
from unittest.mock import MagicMock

from quart import Quart

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import FactDomain
from src.services.biographical_context_service import BiographicalContextService
from src.services.cabinet_fact_service import CabinetFactService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import make_fact
from tests.support.fact_store import FACTS, FactStore, make_env_config
from src.composition.connector_registry import ConnectorRegistry

_USER_ID = "user-1"
_ACCOUNT_ID = "account-1"
_CONTEXT = "eval_user_context"


def _repo(store):
    service = BiographicalContextService(None)
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder,
                                   biographical_context_service=service)
    service.set_repository(repo)
    return repo


def _app(fact_repo):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(
        return_value={"sub": _USER_ID, "account_id": _ACCOUNT_ID, "role": "owner"})
    # Reviewer edit (facts S4, T2c): the route delegates to CabinetFactService, which the blueprint now takes.
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=fact_repo, embedding_service=MagicMock(), cabinet_facts=CabinetFactService(fact_repo), connectors=ConnectorRegistry(),
    )
    app = Quart("test_app")
    app.register_blueprint(bp)
    return app


def _cached_texts(store):
    return [f["text"] for f in store.db.collection(_CONTEXT).docs[_ACCOUNT_ID]["biographical_facts"]]


async def _seed(store, repo):
    for i, text in enumerate(("keep me", "drop me")):
        await store.repo.add_fact(make_fact(text, account=_ACCOUNT_ID, domain=FactDomain.BIOGRAPHICAL, age_days=i))
    await repo.refresh_biographical_context_cache(_ACCOUNT_ID)
    assert sorted(_cached_texts(store)) == ["drop me", "keep me"]


async def _invalidate(app, fact_id):
    async with app.test_client() as client:
        return await client.post(f"/api/user/facts/{fact_id}/invalidate", headers={"Authorization": "Bearer t"})


async def test_invalidated_fact_leaves_the_cached_baseline_immediately():
    store = FactStore()
    repo = _repo(store)
    await _seed(store, repo)

    resp = await _invalidate(_app(repo), "f-drop me")

    assert resp.status_code == 200
    assert _cached_texts(store) == ["keep me"]


async def test_refresh_failure_is_logged_and_does_not_fail_the_invalidate(caplog):
    # The port promises that the refresh never raises (FactRepository.refresh_biographical_context_cache, "Errors").
    # So the failure is driven inside the real repository (its facts read fails during the rebuild), not by replacing
    # the method with one that breaks the contract: the invalidation stands, the error is logged, the old cache stays.
    store = FactStore()
    repo = _repo(store)
    await _seed(store, repo)
    store.db.collection(FACTS).fail = RuntimeError("facts read down")

    with caplog.at_level(logging.ERROR):
        resp = await _invalidate(_app(repo), "f-drop me")

    assert resp.status_code == 200
    assert store.docs["f-drop me"]["state"] == "invalidated"
    assert any("Failed to refresh biographical cache" in r.getMessage() for r in caplog.records)
    assert sorted(_cached_texts(store)) == ["drop me", "keep me"]


async def test_failed_invalidate_does_not_touch_the_cache():
    store = FactStore()
    repo = _repo(store)
    await _seed(store, repo)

    resp = await _invalidate(_app(repo), "no-such-fact")

    assert resp.status_code == 404
    assert sorted(_cached_texts(store)) == ["drop me", "keep me"]
