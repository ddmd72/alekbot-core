"""The connect start route refuses a kind whose spec is not offered (a hand-typed link must not start a flow before the
kind is offered), and still starts an offered one. Over the real stack of `connect_world.py`."""
import pytest

from tests.support.firestore_eval import eval_transactions
from tests.unit.web.connect_world import ConnectWorld


@pytest.fixture
def world(monkeypatch):
    with eval_transactions():
        yield ConnectWorld(monkeypatch)


async def test_a_kind_that_is_not_offered_is_refused_and_stores_no_flow(world):
    assert not world.registry.get("google_tasks").offered
    resp, state = await world.start("google_tasks")
    assert resp.status_code == 404 and state == ""
    assert world.flows == {}


@pytest.mark.parametrize("kind", ["microsoft_onedrive", "google_drive"])   # google_drive offered from slice D (GE-24)
async def test_an_offered_kind_starts_its_flow(world, kind):
    assert world.registry.get(kind).offered
    resp, state = await world.start(kind)
    assert resp.status_code == 302 and state
    assert len(world.flows) == 1
