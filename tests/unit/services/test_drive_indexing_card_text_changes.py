"""Card outcomes when the card text is not the one embedded (USER_DRIVE_INDEXING_RFC §8.2, §8.5): a terminal
refusal binds one text only, and a vector is stored only over the text it was computed from."""
from dataclasses import replace

from src.domain.drive_card import card_text
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.exceptions import LLMClientError

from .drive_index_fakes import FakeEmbeddings
from .test_drive_indexing_service import World


def _refusing(w, prefix, on_refusal=None):
    real = w.emb.get_embedding
    refused = []

    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        if text.startswith(prefix):
            refused.append(text)
            if on_refusal is not None:
                await on_refusal(len(refused))
            raise LLMClientError("400 INVALID_ARGUMENT", http_status=400)
        return await real(text, task_type)

    w.emb.get_embedding = emb
    return refused


async def _cycles(w, n):
    for _ in range(n):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())


async def _rename_in_index(w, item_id, name):
    """The stored record changes while its embedding is in flight (a nudge, a parallel page)."""
    await w.repo.upsert_records([replace(w.rec(item_id), name=name)])


class TestTerminalRefusalBindsOneText:

    async def test_a_renamed_record_gets_the_full_retry_budget_for_its_new_text(self):
        w = World()
        bad = w.drive.add("weird.pdf")
        refused = _refusing(w, "weird")
        await _cycles(w, MAX_ATTEMPTS + 2)
        assert w.rec(bad).card_refused and len(refused) == MAX_ATTEMPTS
        w.drive.rename(bad, "weird-v2.pdf")      # still refused by the provider
        await _cycles(w, MAX_ATTEMPTS + 2)
        assert [t for t in refused if t.startswith("weird-v2")] == [card_text(w.rec(bad))] * MAX_ATTEMPTS
        assert w.rec(bad).card_refused and w.rec(bad).state is RecordState.METADATA_ONLY

    async def test_the_last_refusal_of_a_text_that_changed_meanwhile_is_not_terminal(self):
        w = World()
        bad = w.drive.add("weird.pdf")

        async def rename_on_last(n):
            if n == MAX_ATTEMPTS:
                await _rename_in_index(w, bad, "accepted.pdf")

        _refusing(w, "weird", rename_on_last)
        await _cycles(w, MAX_ATTEMPTS + 2)
        rec = w.rec(bad)
        assert rec.state is not RecordState.FAILED and not rec.card_refused
        assert bad in w.carded() and rec.has_card


class TestVectorMatchesItsText:

    async def test_a_vector_of_a_text_that_changed_mid_batch_is_not_stored(self):
        w = World()
        item = w.drive.add("draft.pdf")
        real = w.emb.get_embedding
        renamed = {"done": False}

        async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
            vec = await real(text, task_type)
            if text.startswith("draft.pdf") and not renamed["done"]:
                renamed["done"] = True
                await _rename_in_index(w, item, "final.pdf")
            return vec

        w.emb.get_embedding = emb
        await w.settle()
        rec = w.rec(item)
        assert rec.has_card and card_text(rec).startswith("final.pdf")
        assert w.repo.vectors[("u", item)] == await FakeEmbeddings().get_embedding(card_text(rec))
