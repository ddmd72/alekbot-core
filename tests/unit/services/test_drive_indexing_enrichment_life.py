"""Search v2 P5 — a file's life through the tick (USER_DRIVE_INDEXING_RFC §25.4.5-§25.4.9): new file -> content ->
profile -> passages -> an edit the profile input does not see (no re-profile, hints reused, the rest after the
cooldown) -> rename -> delete. On the in-memory repository with the scripted profiler and passages."""
import pytest

from src.domain.drive_card import card_text
from src.domain.drive_enrichment import JobState
from src.domain.drive_index import KEYS_VERSION, PASSAGES_VERSION, PROFILE_VERSION, RecordState
from src.domain.drive_index_params import PASSAGE_REFRESH_COOLDOWN, PROFILE_HEAD_CHARS, PROFILE_TAIL_CHARS

from .drive_content_world import admitted  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld

pytestmark = pytest.mark.usefixtures("admitted")


_WORDS = ("garden", "window", "heating", "deposit", "kitchen", "balcony", "parking", "insurance", "repairs",
          "utilities", "furniture", "notice", "keys", "pets", "smoking", "visitors", "rent", "water")


def paragraphs(n: int, changed: range = range(0)) -> str:
    """Varied prose (the machine-text gate passes it): the `changed` paragraphs swap one word for a same-length one,
    so the chunk boundaries stay and only the chunks holding them change."""
    out = []
    for i in range(n):
        lead = "Annexe" if i in changed else "Clause"
        sentences = [f"{lead} {i}.{j} covers the {_WORDS[(i * 7 + j) % len(_WORDS)]} and the "
                     f"{_WORDS[(i * 3 + j * 5) % len(_WORDS)]} of the flat, agreed on day {(i + j) % 28 + 1}."
                     for j in range(6)]
        out.append(" ".join(sentences))
    return "Acme Rentals lease.\n\n" + "\n\n".join(out)


async def test_a_files_life():
    w = EnrichWorld()
    text = paragraphs(30)
    assert len(text) > PROFILE_HEAD_CHARS + PROFILE_TAIL_CHARS + 3000   # an edit in the middle is unseen
    doc = w.add_doc("lease.md", text)

    # -- new file: content, profile and passages, its card embedded once with the profile --------------------------
    await w.settle()
    rec = w.rec(doc)
    assert rec.state is RecordState.INDEXED and rec.keys_version == KEYS_VERSION
    assert all(c.word_keys and c.keys_version == KEYS_VERSION and c.content_terms == () for c in w.readable(doc))
    assert rec.profile is not None and rec.profile.what_it_is == "A document about Acme"
    assert (rec.profile_state, rec.profile_version, rec.profile_fingerprint) == (
        JobState.CURRENT.value, PROFILE_VERSION, rec.profile_input_fingerprint)
    assert "e:acme" in rec.about_keys and "l:type:contract" in rec.about_keys
    assert (rec.passages_state, rec.passages_version, rec.passages_generation) == (
        JobState.CURRENT.value, PASSAGES_VERSION, rec.indexed_generation)
    assert [h.index for h in w.hints(doc)] == list(range(rec.chunk_count))
    assert all(h.hint_keys and h.items[0].evidence_start >= 0 for h in w.hints(doc))
    assert len(w.profiler.requests) == 1 and w.profiler.requests[0].kind == "markdown"
    cards = [t for t in w.emb.calls if t.startswith("lease.md | ")]
    assert cards == [card_text(rec)] and "A document about Acme" in cards[0]   # carded once, with its profile
    assert not rec.enrich_ready and rec.enrich_wake_at is None
    calls_before = len(w.passages.requests)
    old_gen = rec.indexed_generation
    ticks_before = len(w.results)

    # -- an edit in the middle: the profile input is unchanged, so no re-profile; hints reused --------------------
    w.drive.edit(doc, paragraphs(30, changed=range(13, 18)).encode(), modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    rec = w.rec(doc)
    assert rec.indexed_generation != old_gen and w.hint_generations(doc) == [rec.indexed_generation]
    assert len(w.profiler.requests) == 1                               # the skip rule (§25.4.7)
    assert rec.profile_state == JobState.CURRENT.value and rec.profiled_content_version == rec.content_version
    # the edit's ticks only, each counted once (P5 review MINOR 11: the last tick was counted twice)
    reused = sum(r.summary.get("hints_reused", 0) for r in w.results[ticks_before:])
    assert reused >= rec.chunk_count - 2
    assert len(w.passages.requests) == calls_before                    # the changed chunk waits for the cooldown
    assert rec.passages_state == JobState.DEFERRED.value
    missing = set(range(rec.chunk_count)) - {h.index for h in w.hints(doc)}
    assert missing and len(missing) <= 2

    w.clock.advance(PASSAGE_REFRESH_COOLDOWN.total_seconds())
    await w.settle()
    rec = w.rec(doc)
    assert rec.passages_state == JobState.CURRENT.value
    assert [h.index for h in w.hints(doc)] == list(range(rec.chunk_count))
    sent = {i for r in w.passages.requests[calls_before:] for i, _ in r.chunks}
    assert sent == missing                                             # only the chunks without hints

    # -- rename: no re-profile, no re-read, new name keys -------------------------------------------------------
    downloads = len(w.drive.downloads)
    w.drive.rename(doc, "contract 2026.md")
    await w.settle()
    rec = w.rec(doc)
    assert len(w.drive.downloads) == downloads and len(w.profiler.requests) == 1
    assert "w:contract" in rec.name_tokens and rec.profile_state == JobState.CURRENT.value

    # -- delete: the record, its chunks and its hint documents go ------------------------------------------------
    w.drive.delete(doc)
    await w.settle()
    assert w.rec(doc) is None and w.chunks(doc) == [] and w.hint_generations(doc) == []
