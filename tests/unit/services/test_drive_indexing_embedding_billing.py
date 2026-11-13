"""A drive tick bills its embeddings to the connection's account: one write per model per tick, never per call
(decisions/billing_embeddings.md)."""
from src.domain.billing import calculate_cost, current_ledger

from .drive_index_fakes import FakeEmbeddings
from .test_drive_indexing_service import World

TOKENS_PER_CALL = 5


class MeteredEmbeddings(FakeEmbeddings):
    """Models the adapter: every call adds its prompt tokens to the ledger in scope."""

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        vector = await super().get_embedding(text, task_type)
        ledger = current_ledger()
        if ledger is not None:
            ledger.add("gemini-embedding-2", prompt_tokens=TOKENS_PER_CALL)
        return vector


class RecordingQuota:
    def __init__(self):
        self.calls = []

    async def record_usage(self, account_id, model, tokens, cost):
        self.calls.append((account_id, model, tokens, cost))


async def test_one_tick_with_many_embeddings_writes_one_record_for_the_account():
    w = World()
    w.emb = MeteredEmbeddings()
    w.svc._embeddings = w.emb
    quota = RecordingQuota()
    w.svc._quota = quota
    for i in range(4):
        w.drive.add(f"doc{i}.pdf")
    await w.tick()
    assert len(w.emb.calls) >= 4
    total = TOKENS_PER_CALL * len(w.emb.calls)
    assert quota.calls == [("a", "gemini-embedding-2", total, calculate_cost("gemini-embedding-2", total, 0))]


async def test_without_a_quota_service_the_tick_still_runs():
    w = World()
    w.emb = MeteredEmbeddings()
    w.svc._embeddings = w.emb
    w.drive.add("doc.pdf")
    await w.tick()
    assert w.emb.calls
