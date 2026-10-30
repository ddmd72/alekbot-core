"""The connect callback's two refusals left open by slice 1a: a session token without a string subject is no session,
and a connect service that raises ends in the `exchange` error with nothing stored. World: connect_world.py."""
import logging
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import SECRET, ConnectWorld


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        yield ConnectWorld(monkeypatch)


def _token(**claims) -> str:
    payload = {"type": "access", "account_id": "acct-1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    payload.update(claims)
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, SECRET, algorithm="HS256")


@pytest.mark.parametrize("claims", [{}, {"sub": 42}, {"sub": ""}])
async def test_a_session_token_without_a_string_subject_is_no_session(world, claims):
    _, state = await world.start("gmail")
    resp = await world.callback(state, token=_token(**claims))
    assert resp.status_code == 302 and resp.headers["Location"] == "/auth/login?next=/cabinet"
    assert await world.held("gmail") == [] and len(world.flows) == 1       # the flow was never consumed


async def test_a_connect_service_failure_redirects_with_the_exchange_error(world, caplog):
    _, state = await world.start("gmail")

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")

    world.connect_service.complete = broken
    with caplog.at_level(logging.ERROR):
        resp = await world.callback(state)
    assert resp.headers["Location"] == "/cabinet?connect_error=exchange"
    assert await world.held("gmail") == []
    assert "Integration connect failed" in " ".join(r.getMessage() for r in caplog.records)
