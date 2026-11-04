"""`DriveSearchService.excerpts` with `caps` and `batch` (DRIVE_MCP_CONNECTOR_RFC §14.2-§14.4): batch k is what the k-th
of k identical calls returns when their results feed one `ExcerptLedger` (the chat's specialist), with the pool and the
ranking run once; a chunk the merge dropped and a chosen chunk whose text read failed stay selectable; a batch past the
end stops early and selects nothing new; `more` says whether a later batch could return more; `wide` doubles the
passages per call; `batch=1` with the default caps is the chat's call, read for read. The real in-memory repository
and fixed vectors; invented items only."""
from __future__ import annotations

from typing import List, Set, Tuple

from src.domain.drive_excerpt_caps import ExcerptCaps
from src.domain.drive_excerpt_ledger import ExcerptLedger
from src.domain.drive_excerpt_render import excerpt_plan, render_excerpts
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_index_params import EXCERPTS_K
from src.domain.drive_search import STATUS_OK
from tests.unit.domain.drive_excerpt_builders import nonces

from .drive_excerpt_batches_harness import FlakyRepo
from .test_drive_search_excerpts import QUESTION, CountingRepo, PassageWorld
from .test_drive_search_excerpts_across_drives import Passages


def shown_of(result) -> List[Tuple[str, str, int, str]]:
    return [(f.ref_tag, f.item_id, c.index, c.text) for f in result.files for c in f.chunks]


async def sequential(find, k: int):
    """The chat's loop: k identical finds, each result recorded by one ledger as the specialist records it (the
    render's plan over the ledger's context), the next find's `shown` its keys. The k-th result and the ledger's
    context before it."""
    ledger = ExcerptLedger()
    result = None
    seen = (frozenset(), frozenset())
    for _ in range(k):
        seen = (ledger.folders_shown(), ledger.files_described())
        result = await find(ledger.keys())
        plan = render_excerpts(result, nonce=nonces(), shown_folders=seen[0], described_files=seen[1]).plan
        ledger.add(result, plan)
    return result, seen


def one_drive(world: PassageWorld, files: int, chunks: int) -> List[str]:
    """`files` documents of `chunks` chunks each, every chunk at its own distance (the first file nearest)."""
    items = []
    for f in range(files):
        texts = [f"Step {i + 1} of the review in part {f + 1} of Tom's plan for Acme Hamburg." for i in range(chunks)]
        items.append(world.doc(f"Tom's plan part {f + 1}.docx", texts, [0.05 * (f * chunks + i + 1)
                                                                          for i in range(chunks)]))
    return items


def chunks_in(result) -> Set[Tuple[str, int]]:
    return {(f.item_id, c.index) for f in result.files for c in f.chunks}


# -- one connection -----------------------------------------------------------------------------------------------

async def test_batch_k_is_the_kth_of_k_identical_calls_through_one_ledger():
    w = PassageWorld()
    one_drive(w, 4, 6)   # 24 chunks: three batches of k and a fourth with the rest
    for k in (1, 2, 3):
        replayed = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=k)
        chat, seen = await sequential(lambda keys: w.find(shown=keys), k)
        assert shown_of(replayed) == shown_of(chat) and shown_of(replayed)
        assert replayed.repeated == chat.repeated and replayed.more is chat.more is (k < 3)
        assert (replayed.seen_folders, replayed.seen_files) == seen


async def test_the_batches_together_are_every_ranked_chunk_once():
    w = PassageWorld()
    items = one_drive(w, 3, 5)
    got: List[Tuple[str, int]] = []
    for k in (1, 2):
        got.extend((f.item_id, c.index) for f in (await w.service.excerpts(
            "u", ExcerptRequest(question=QUESTION), batch=k)).files for c in f.chunks)
    assert sorted(got) == sorted((item, i) for item in items for i in range(5))
    last = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    assert last.more is False


async def test_batch_one_with_the_default_caps_is_the_chats_call_read_for_read():
    chat_world, batch_world = PassageWorld(), PassageWorld()
    one_drive(chat_world, 3, 6)
    one_drive(batch_world, 3, 6)
    chat = await chat_world.find()
    same = await batch_world.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=1,
                                              caps=ExcerptCaps.DEFAULT)
    assert shown_of(same) == shown_of(chat) and same == chat
    assert batch_world.repo.calls == chat_world.repo.calls and batch_world.repo.knn_limits == chat_world.repo.knn_limits


async def test_the_pool_and_the_ranking_run_once_for_any_batch():
    w = PassageWorld()
    one_drive(w, 4, 6)
    await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=3)
    assert len(w.repo.knn_limits) == 1 and w.repo.calls["keys"] == 1


async def test_a_batch_past_the_end_stops_early_and_selects_nothing_new():
    w = PassageWorld()
    one_drive(w, 2, 5)
    past = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=40)
    assert past.status == STATUS_OK and past.files == () and past.more is False
    assert len(past.repeated) == 10                                  # every chunk shown by the earlier batches
    reads_past = w.repo.calls["texts"]
    second = PassageWorld()
    one_drive(second, 2, 5)
    await second.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    assert reads_past == second.repo.calls["texts"]                  # the rounds up to the end, no more


async def test_a_chunk_whose_text_read_failed_stays_selectable():
    def build():
        w = PassageWorld()
        flaky = FlakyRepo.over(w.repo)
        w.repo = flaky
        w.service._repo = flaky
        items = one_drive(w, 3, 4)   # 12 chunks
        flaky.fail_once.add((items[0], 0))
        return w, items

    w, items = build()
    first = await w.service.excerpts("u", ExcerptRequest(question=QUESTION))
    assert (items[0], 0) not in chunks_in(first) and len(chunks_in(first)) == EXCERPTS_K and first.more is True
    w, items = build()
    second = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    w, _ = build()
    chat, _ = await sequential(lambda keys: w.find(shown=keys), 2)
    assert (items[0], 0) in chunks_in(second) and shown_of(second) == shown_of(chat)


async def test_wide_selects_twice_as_many_passages_per_call():
    w = PassageWorld()
    one_drive(w, 5, 5)
    narrow = await w.service.excerpts("u", ExcerptRequest(question=QUESTION))
    wide = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), caps=ExcerptCaps.WIDE)
    assert len(chunks_in(narrow)) == EXCERPTS_K and len(chunks_in(wide)) == ExcerptCaps.WIDE.k
    assert chunks_in(narrow) < chunks_in(wide) and wide.more is True
    rest = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), caps=ExcerptCaps.WIDE, batch=2)
    assert len(chunks_in(rest)) == 5 and rest.more is False and not chunks_in(rest) & chunks_in(wide)


async def test_the_earlier_batches_context_is_carried_as_seen():
    w = PassageWorld()
    rd = w.folder("R&D")
    w.note(rd, "Acme Hamburg specifications")
    for f in range(3):
        w.doc(f"Tom's plan part {f + 1}.docx", [f"Step {i + 1} of the first review." for i in range(5)],
              [0.05 * (f * 5 + i + 1) for i in range(5)], parent=rd)
    first = await w.service.excerpts("u", ExcerptRequest(question=QUESTION))
    second = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    plan = excerpt_plan(first)
    assert first.seen_folders == frozenset() == first.seen_files
    assert second.seen_folders == plan.shown_after == set(first.context.folders) and len(plan.shown_after) == 1
    assert second.seen_files == plan.described and plan.described


# -- two connections ------------------------------------------------------------------------------------------------

async def _two_drives() -> Passages:
    w = await Passages().open()
    for f in range(3):
        w.doc(f"Tom's plan part {f + 1}.docx", [f"Part {f + 1}, step {i + 1} of the review." for i in range(3)],
              [0.1 * (2 * f + 1) + 0.01 * i for i in range(3)])
        w.on_acme(w.doc(f"Acme review {f + 1}.docx", [f"Acme review {f + 1}, sign-off {i + 1}." for i in range(3)],
                        [0.1 * (2 * f + 2) + 0.01 * i for i in range(3)]))
    return w


async def test_a_chunk_the_merge_dropped_stays_selectable_for_the_next_batch():
    w = await _two_drives()   # each drive selects its 9 chunks; the merge keeps 10 of the 18
    first = await w.passages()
    assert len(chunks_in(first)) == EXCERPTS_K and first.more is True
    replayed = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=2)
    chat, _ = await sequential(lambda keys: w.passages(shown=keys), 2)
    assert shown_of(replayed) == shown_of(chat)
    assert len(chunks_in(replayed)) == 8 and not chunks_in(replayed) & chunks_in(first)
    assert {f.ref_tag for f in replayed.files} == {"a1", "b2"} and replayed.more is False


async def test_two_connections_past_the_end_answer_nothing_new():
    w = await _two_drives()
    past = await w.service.excerpts("u", ExcerptRequest(question=QUESTION), batch=5)
    assert past.files == () and past.more is False and len(past.repeated) == 18


def test_the_counting_repo_is_the_harness_base():
    assert issubclass(FlakyRepo, CountingRepo)
