"""The indexer's `/worker` drive_sync by connection (MULTI_DRIVE_RFC §8): the tick runs for the payload's
`ConnectionScope`; a payload without a valid `user_id` and `connection_id` is refused (400) and no tick runs. A fake
tick service records what it was asked, no mock assertion. Technical ids only."""
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import Nudge
from src.domain.drive_task_keys import scope_payload
from src.services.drive_indexing_service import TickResult
from src.web.indexer_worker_app import create_indexer_worker_blueprint

SCOPE = ConnectionScope(user_id="usr-1", connection_id="conn-1")


class Ticks:
    def __init__(self) -> None:
        self.asked: List[Tuple[ConnectionScope, Optional[Nudge]]] = []

    async def tick(self, scope: ConnectionScope, nudge: Optional[Nudge] = None) -> TickResult:
        self.asked.append((scope, nudge))
        return TickResult("done", {"observed": 1})


def _app():
    ticks = Ticks()
    app = Quart(__name__)
    app.register_blueprint(create_indexer_worker_blueprint(ticks, AsyncMock(return_value=True)))   # type: ignore[arg-type]
    return app, ticks


async def test_the_tick_runs_for_the_payloads_connection():
    app, ticks = _app()
    nudge = Nudge(mutation_id="m1", item_ids=("item-1",), attempt=0)
    resp = await app.test_client().post("/worker", json={"task_type": "drive_sync", **scope_payload(SCOPE, nudge)})
    assert resp.status_code == 200 and (await resp.get_json())["status"] == "done"
    assert ticks.asked == [(SCOPE, nudge)]


@pytest.mark.parametrize("payload", [
    {"user_id": "usr-1"},                                   # the single-drive payload: no connection
    {"connection_id": "conn-1"},
    {"user_id": "usr-1", "connection_id": ""},
    {"user_id": "usr-1", "connection_id": "a:b"},          # not a connection id
    {"user_id": 7, "connection_id": "conn-1"},
])
async def test_a_payload_without_a_connection_is_refused(payload):
    app, ticks = _app()
    resp = await app.test_client().post("/worker", json={"task_type": "drive_sync", **payload})
    assert resp.status_code == 400 and ticks.asked == []
