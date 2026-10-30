"""Card embedding outcomes in the tick (USER_DRIVE_INDEXING_RFC §8.2, §12.3): a card the provider keeps refusing
becomes terminal, and a systemic failure mid-batch keeps the vectors already obtained."""
import pytest
from src.domain import drive_card
from src.domain import drive_index_planner as planner
from src.domain.drive_index import CURRENT_VERSIONS, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.exceptions import LLMClientError, LLMServerError
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import U_SCOPE


def _bump_card_version(monkeypatch):
    """CARD_VERSION + 1; returns the version map the service applies once the pass is clean (CORE-DELTA 5 keys)."""
    bumped = drive_card.CARD_VERSION + 1
    monkeypatch.setattr(drive_card, "CARD_VERSION", bumped)
    monkeypatch.setattr(planner, "CARD_VERSION", bumped)
    target = {**CURRENT_VERSIONS, "card": bumped}
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", target)
    return {**target, **EXTRACTOR_VERSIONS}


def _refuse_cards_starting_with(w, prefix):
    real = w.emb.get_embedding
    refused = []

    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        if text.startswith(prefix):
            refused.append(text)
            raise LLMClientError("400 INVALID_ARGUMENT", http_status=400)
        return await real(text, task_type)

    w.emb.get_embedding = emb
    return refused


def _count_upgrade_pages(w):
    pages = []
    orig = w.repo.records_page

    async def counting(user_id, after, limit):
        pages.append(after)
        return await orig(user_id, after, limit)

    w.repo.records_page = counting
    return pages


class TestRefusedCardBecomesTerminal:

    @pytest.mark.slow
    async def test_upgrade_passes_stop_once_the_refused_card_is_terminal(self, monkeypatch):
        """After a CARD_VERSION bump one card is refused on every attempt. It is refused for good after MAX_ATTEMPTS,
        the bump applies, and the fan-out's quiet ticks read no upgrade page any more."""
        w = World(page_size=500)
        for i in range(300):
            w.drive.add(f"doc{i:03d}.pdf")
        bad = w.drive.add("weird.pdf")
        await w.settle()
        target = _bump_card_version(monkeypatch)
        refused = _refuse_cards_starting_with(w, "weird.pdf")
        pages = _count_upgrade_pages(w)
        await w.settle()
        per_tick = []
        for _ in range(12):                      # two hours of fan-out ticks; nothing changes in the drive
            before = len(pages)
            w.clock.advance(600)
            await w.tick()
            per_tick.append(len(pages) - before)
        st = await w.repo.get_sync_state(U_SCOPE)
        assert dict(st.applied_versions) == target
        assert len(refused) == MAX_ATTEMPTS
        assert per_tick[-6:] == [0] * 6, per_tick
        rec = w.rec(bad)
        assert rec.card_refused and rec.state is RecordState.METADATA_ONLY and not rec.has_card
        assert bad not in w.carded()
        assert rec.card_version == drive_card.CARD_VERSION and not planner.stale_versions(rec) and rec.due_at is None

    @pytest.mark.slow
    async def test_a_backed_off_stale_record_costs_no_pass_per_quiet_tick(self, monkeypatch):
        """While the refused card waits for its retry, a quiet tick does not re-read the index in a new pass."""
        w = World(page_size=500)
        for i in range(300):
            w.drive.add(f"doc{i:03d}.pdf")
        bad = w.drive.add("weird.pdf")
        await w.settle()
        target = _bump_card_version(monkeypatch)
        _refuse_cards_starting_with(w, "weird.pdf")
        await w.settle()
        assert w.rec(bad).attempts == 1 and w.rec(bad).due_at > w.clock.now
        pages = _count_upgrade_pages(w)
        for _ in range(5):
            w.clock.advance(60)
            await w.tick()
        assert pages == []
        assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions) != target

    async def test_a_terminal_refusal_is_not_retried_until_the_card_text_changes(self):
        w = World()
        bad = w.drive.add("weird.pdf")
        refused = _refuse_cards_starting_with(w, "weird")
        for _ in range(MAX_ATTEMPTS + 2):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())
        assert len(refused) == MAX_ATTEMPTS and w.rec(bad).card_refused
        assert w.rec(bad).state is RecordState.METADATA_ONLY
        w.drive.reemit(bad)                      # delta re-emits unchanged items often (§7.2)
        await w.settle()
        assert len(refused) == MAX_ATTEMPTS
        assert w.rec(bad).card_refused and w.rec(bad).state is RecordState.METADATA_ONLY
        w.drive.rename(bad, "renamed.pdf")       # a new card text gets a fresh try
        await w.settle()
        assert bad in w.carded() and not w.rec(bad).card_refused and w.rec(bad).state is RecordState.METADATA_ONLY

    async def test_a_card_builder_bump_retries_a_terminal_refusal(self, monkeypatch):
        w = World()
        bad = w.drive.add("weird.pdf")
        refused = _refuse_cards_starting_with(w, "weird")
        for _ in range(MAX_ATTEMPTS + 2):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())
        assert w.rec(bad).card_refused and w.rec(bad).state is RecordState.METADATA_ONLY
        _bump_card_version(monkeypatch)
        del w.emb.get_embedding                  # the provider accepts the new card builder's text
        await w.settle()
        assert bad in w.carded() and w.rec(bad).state is RecordState.METADATA_ONLY and len(refused) == MAX_ATTEMPTS
        assert not w.rec(bad).card_refused


class TestSystemicEmbeddingFailureKeepsTheBatch:

    async def test_one_poison_card_does_not_discard_the_other_vectors(self):
        """A 5xx is systemic (§8.2) and ends the tick, but the vectors the batch already holds are committed."""
        w = World(page_size=500)
        for i in range(50):   # .doc: no content run ahead of the cards (§7.1), so one batch holds all 51 cards
            w.drive.add(f"doc{i:02d}.doc")
        poison = w.drive.add("doc00 poison.doc")
        real = w.emb.get_embedding

        async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
            if "poison" in text:
                raise LLMServerError("500 Internal error", http_status=500)
            return await real(text, task_type)

        w.emb.get_embedding = emb
        result = await w.tick()
        assert result.status in ("error", "throttled")
        assert len(w.carded()) == 50 and poison not in w.carded()
        assert all(w.rec(i).has_card for i in w.carded())
        assert w.rec(poison).attempts == 0       # systemic: attempts untouched

    async def test_refusals_in_a_systemically_failed_batch_are_recorded(self):
        w = World(page_size=500)
        for i in range(5):
            w.drive.add(f"doc{i}.pdf")
        refused_item = w.drive.add("refused.pdf")
        w.drive.add("poison.pdf")
        real = w.emb.get_embedding

        async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
            if text.startswith("poison"):
                raise LLMServerError("500 Internal error", http_status=500)
            if text.startswith("refused"):
                raise LLMClientError("400 INVALID_ARGUMENT", http_status=400)
            return await real(text, task_type)

        w.emb.get_embedding = emb
        await w.tick()
        assert w.rec(refused_item).attempts == 1 and w.rec(refused_item).due_at > w.clock.now
        assert len(w.carded()) == 5
