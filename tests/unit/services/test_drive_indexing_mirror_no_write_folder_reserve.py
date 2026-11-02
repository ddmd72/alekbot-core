"""A connection with no write folder (read-only, MULTI_DRIVE_RFC §11.3, D7) takes no transcript reserve in the tick: the
mirror has no work it could do there. The real service, stage and mirror step on the in-memory repositories."""
from dataclasses import replace
from typing import Any

import pytest

from src.domain.drive_index_params import MIRROR_RESERVE_S

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_step_world import StepWorld

pytestmark = pytest.mark.usefixtures("admitted")


def _read_only(w: StepWorld) -> None:
    connections = w.svc._connections
    real_get = connections.get

    async def get(user_id: str, connection_id: str) -> Any:
        found = await real_get(user_id, connection_id)
        return replace(found, write_folder_id=None) if found is not None else None
    connections.get = get   # type: ignore[method-assign]


async def test_a_read_only_connection_takes_no_transcript_reserve(tmp_path):
    w = StepWorld(tmp_path)
    w.add_doc("notes.md", prose(5))
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S      # a writable connection: root setup is due, reserve taken
    w.mirror.root_docs.clear()
    _read_only(w)
    w.clock.advance(60)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0
    assert w.root_doc() is None
