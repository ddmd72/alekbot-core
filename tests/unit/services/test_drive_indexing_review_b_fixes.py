"""Index-run fixes from adversarial review B (USER_DRIVE_INDEXING_RFC §8.1, §8.2, §12.3, §24.5, §24.7): CPU work off
the event loop, the month budget, the chunk embedding space, and which errors are systemic."""
import threading

import pytest

from src.domain.drive_index import RecordState
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ContentWorld()


# -- B-M1: the gate, the chunker and the terms run in a worker thread ----------------------------------------------

async def test_gate_chunker_and_terms_run_off_the_event_loop(w, monkeypatch):
    on_loop = []

    def watched(fn):
        def run(*args, **kwargs):
            on_loop.append((fn.__name__, threading.current_thread() is threading.main_thread()))
            return fn(*args, **kwargs)
        return run

    for name in ("is_machine_text", "chunk_text", "content_terms"):
        monkeypatch.setattr(svc_mod, name, watched(getattr(svc_mod, name)))
    doc = w.add_doc("notes.md", prose(80))
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED
    assert {name for name, _ in on_loop} == {"is_machine_text", "chunk_text", "content_terms"}
    assert not any(main for _, main in on_loop)
