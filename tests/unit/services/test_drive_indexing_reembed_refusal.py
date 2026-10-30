"""A chunk the new embedding space refuses (HTTP 400) is terminal for that space (R3 F2-3, USER_DRIVE_INDEXING_RFC
§8.2, §12.3), like a refused card text: no re-read and no reader call, the old-space chunks are collected, the record
no longer counts as stale in that space, and the space change completes, so card and chunk kNN come back for the
user. A later space gives the file a fresh try."""
import pytest

from src.domain.drive_index import Reason, RecordState
from src.domain.drive_index_params import RETRY_CAP
from src.domain.exceptions import LLMClientError
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

NEW = "new-space/768"
LATER = "later-space/768"


class RefusingEmbeddings:
    """Refuses every text in `refused` with a definite bad-input 400; embeds anything else."""

    def __init__(self, inner, refused):
        self.inner = inner
        self.refused = set(refused)
        self.refusals = 0

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        if text in self.refused:
            self.refusals += 1
            raise LLMClientError("400 INVALID_ARGUMENT", http_status=400)
        return await self.inner.get_embedding(text, task_type)


def _world():
    w = ContentWorld()

    def ticking():
        w.clock.mono += 0.05
        return w.clock.mono

    w.clock.monotonic = ticking
    w.svc._monotonic = ticking
    return w


def _switch_space(w, space, embeddings):
    w.svc = svc_mod.DriveIndexingService(w.drive, w.repo, embeddings, w.queue, w.users, lambda lat, lon: None, space,
                                         extractors=w.svc._extractors, triage=w.svc._triage, connections=LegacyConnections(w.users, w.drive), clock=w.clock,
                                         monotonic=w.clock.monotonic)


async def test_a_chunk_the_new_space_refuses_is_terminal_for_that_space():
    w = _world()
    doc = w.add_doc("notes.md", prose(150))
    await w.settle()
    chunk_texts = [c.text for c in w.readable(doc)]
    assert chunk_texts
    parses, reads = len(w.parser.requests), len(w.reader.requests)
    refusing = RefusingEmbeddings(w.emb, chunk_texts[:1])
    _switch_space(w, NEW, refusing)
    for _ in range(4):   # several upgrade passes: nothing loops
        await w.settle(max_ticks=20)
        w.clock.advance(RETRY_CAP.total_seconds())
    rec = w.rec(doc)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert len(w.parser.requests) == parses and len(w.reader.requests) == reads   # never read again
    assert refusing.refusals == 1
    assert (rec.state, rec.reason, rec.indexed_generation) == (RecordState.FAILED, Reason.EXTRACT_ERROR, None)
    assert w.chunks(doc) == []                                                    # old-space chunks collected
    assert state.applied_space == NEW and state.cards_in_space(NEW)
    assert rec.has_card and rec.embedding_space == NEW                            # still findable by its card
    # a later space gives it a fresh try: the file is read again and indexed there
    _switch_space(w, LATER, w.emb)
    await w.settle(max_ticks=20)
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.chunk_space == LATER and w.readable(doc)
    assert len(w.parser.requests) == parses + 1
