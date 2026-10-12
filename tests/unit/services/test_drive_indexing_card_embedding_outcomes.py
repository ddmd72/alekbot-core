"""Card embedding outcomes in the tick (USER_DRIVE_INDEXING_RFC §8.2, §12.3): a card the provider keeps refusing
becomes terminal."""
from src.domain import drive_card
from src.domain import drive_index_planner as planner
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.exceptions import LLMClientError
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World


def _bump_card_version(monkeypatch):
    monkeypatch.setattr(drive_card, "CARD_VERSION", 2)
    monkeypatch.setattr(planner, "CARD_VERSION", 2)
    target = {"policy": 1, "card": 2, "geo_table": 1}
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", target)
    return target


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

    async def test_upgrade_passes_stop_once_the_refused_card_is_terminal(self, monkeypatch):
        """After a CARD_VERSION bump one card is refused on every attempt. It turns failed after MAX_ATTEMPTS,
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
        st = await w.repo.get_sync_state("u")
        assert dict(st.applied_versions) == target
        assert len(refused) == MAX_ATTEMPTS
        assert per_tick[-6:] == [0] * 6, per_tick
        rec = w.rec(bad)
        assert rec.state is RecordState.FAILED and not rec.has_card and bad not in w.carded()
        assert rec.card_version == 2 and not planner.stale_versions(rec) and rec.due_at is None

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
        assert dict((await w.repo.get_sync_state("u")).applied_versions) != target

    async def test_a_terminal_refusal_is_not_retried_until_the_card_text_changes(self):
        w = World()
        bad = w.drive.add("weird.pdf")
        refused = _refuse_cards_starting_with(w, "weird")
        for _ in range(MAX_ATTEMPTS + 2):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())
        assert len(refused) == MAX_ATTEMPTS and w.rec(bad).state is RecordState.FAILED
        w.drive.reemit(bad)                      # delta re-emits unchanged items often (§7.2)
        await w.settle()
        assert len(refused) == MAX_ATTEMPTS
        assert w.rec(bad).state is RecordState.FAILED
        w.drive.rename(bad, "renamed.pdf")       # a new card text gets a fresh try
        await w.settle()
        assert bad in w.carded() and w.rec(bad).state is RecordState.METADATA_ONLY

    async def test_a_card_builder_bump_retries_a_terminal_refusal(self, monkeypatch):
        w = World()
        bad = w.drive.add("weird.pdf")
        refused = _refuse_cards_starting_with(w, "weird")
        for _ in range(MAX_ATTEMPTS + 2):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())
        assert w.rec(bad).state is RecordState.FAILED
        _bump_card_version(monkeypatch)
        del w.emb.get_embedding                  # the provider accepts the new card builder's text
        await w.settle()
        assert bad in w.carded() and w.rec(bad).state is RecordState.METADATA_ONLY and len(refused) == MAX_ATTEMPTS
