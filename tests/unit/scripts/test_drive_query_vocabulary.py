"""Vocabulary-miss script (USER_DRIVE_INDEXING_RFC §26.6, Δ66): classification on a recorded synthetic `tool_calls`
sample, per-word counts and the exact-tier check for an opened file. No network: the index is a fake repository."""
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.validation import drive_query_vocabulary as script
from scripts.validation.drive_query_vocabulary_logic import (
    STATUS_ABSENT, STATUS_AUTOMATIC, STATUS_EXACT, STATUS_TRANSLATION, OpenedCase, concepts_of, extract_events,
    open_verdict, pair_searches, probe_plan, ranked_words, render_report, status_of, tally_word, tier_gap,
)
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_match import SourceClass
from src.domain.drive_tiers import FileKeys
from src.ports.drive_index_repository import DriveIndexRepository


def _delegate(intent, **ctx):
    return {"name": "delegate_to_specialist", "args": {"intent": intent, "query": "q", "context": ctx}}


def _row(trace, ts, *calls):
    return {"trace_id": trace, "timestamp": ts, "tool_calls": json.dumps(list(calls))}


SAMPLE = [
    # trace A: search, open, a second search with no open, an unrelated tool
    _row("tr_a", "2026-10-15T10:00:00Z", _delegate("search_files_in_drive", drive_query="zorblax invoice 4417")),
    _row("tr_a", "2026-10-15T10:00:05Z", _delegate("open_file_from_drive", file_ref="drive:ITEM1"),
         {"name": "web_search", "args": {"q": "x"}}),
    _row("tr_a", "2026-10-15T10:01:00Z", _delegate("search_files_in_drive", drive_query="quibbleton")),
    # trace B: filter-only search, a malformed row, a context sent as free text, an open before any search
    _row("tr_b", "2026-10-16T09:00:00Z", _delegate("open_file_from_drive", file_ref="drive:LOOSE")),
    _row("tr_b", "2026-10-16T09:00:10Z", _delegate("search_files_in_drive", drive_kind="pdf")),
    {"trace_id": "tr_b", "timestamp": "2026-10-16T09:00:20Z", "tool_calls": "not json"},
    {"trace_id": "tr_b", "timestamp": "2026-10-16T09:00:30Z", "tool_calls": None},
    {"trace_id": "tr_b", "timestamp": "2026-10-16T09:00:40Z",
     "tool_calls": json.dumps([{"name": "delegate_to_specialist",
                                "args": {"intent": "search_files_in_drive", "context": "free text"}}])},
]


def test_extract_events_orders_by_trace_and_time_and_skips_noise():
    events = extract_events(reversed(SAMPLE))
    assert [(t, e.kind) for t, e in events] == [
        ("tr_a", "search_files_in_drive"), ("tr_a", "open_file_from_drive"), ("tr_a", "search_files_in_drive"),
        ("tr_b", "open_file_from_drive"), ("tr_b", "search_files_in_drive"), ("tr_b", "search_files_in_drive")]
    assert events[0][1].drive_query == "zorblax invoice 4417"
    assert events[1][1].item_id == "ITEM1"


def test_pair_searches_attaches_opens_to_the_search_before_them():
    records = pair_searches(SAMPLE)
    assert [(r.trace_id, r.drive_query, r.opened) for r in records] == [
        ("tr_a", "zorblax invoice 4417", ("ITEM1",)), ("tr_a", "quibbleton", ()),
        ("tr_b", "", ()), ("tr_b", "", ())]      # the open before any search in tr_b belongs to no search


def test_probe_plan_asks_each_stream_per_key_family_with_its_class():
    (number,) = [c for c in concepts_of("4417")[0]]
    plan = {(g.stream, g.prefix): g.source_class for g in probe_plan(number, False)}
    assert plan[("name", "n:")] == SourceClass.VERIFIED_RECORD
    assert plan[("about", "pn:")] == SourceClass.AUTOMATIC_RECORD
    assert plan[("chunk", "n:")] == SourceClass.PASSAGE_WORDS
    (word,) = concepts_of("zorblax")[0]
    plan = {(g.stream, g.prefix): g.source_class for g in probe_plan(word, False)}
    assert plan[("about", "ef:")] == SourceClass.TRANSLATION
    assert plan[("chunk", "x:")] == SourceClass.TRANSLATION
    assert plan[("hint", "w:")] == SourceClass.TRANSLATION
    assert plan[("about", "lw:")] == SourceClass.AUTOMATIC_RECORD
    assert {g.stream for g in probe_plan(word, True)} == {"name"}
    assert all(len(g.keys) <= 30 for g in probe_plan(word, False))


def test_status_is_the_strongest_class_held():
    assert status_of([]) == STATUS_ABSENT
    assert status_of([SourceClass.AUTOMATIC_RECORD]) == STATUS_AUTOMATIC
    assert status_of([SourceClass.AUTOMATIC_RECORD, SourceClass.TRANSLATION]) == STATUS_TRANSLATION
    assert status_of([SourceClass.TRANSLATION, SourceClass.PASSAGE_WORDS]) == STATUS_EXACT
    assert status_of([SourceClass.VERIFIED_RECORD]) == STATUS_EXACT


def test_per_word_counts_and_ranking():
    tallies = {}
    (zor,) = concepts_of("zorblax")[0]
    (quib,) = concepts_of("quibbleton")[0]
    (inv,) = concepts_of("invoice")[0]
    for status in (STATUS_ABSENT, STATUS_ABSENT, STATUS_AUTOMATIC, STATUS_EXACT):
        tally_word(tallies, zor, status)
    tally_word(tallies, quib, STATUS_TRANSLATION)
    tally_word(tallies, inv, STATUS_EXACT)
    assert tallies["zorblax"].queries == 4 and tallies["zorblax"].counts[STATUS_ABSENT] == 2
    assert tallies["zorblax"].missed() == 3
    assert [t.word for t in ranked_words(tallies)] == ["zorblax", "quibbleton"]   # an always-exact word is not listed


def _file_with(concept_keys_by_where):
    return FileKeys(item_id="ITEM1", **concept_keys_by_where)


def test_open_verdict_exact_when_every_concept_is_held_exactly():
    concepts, _, _, _ = concepts_of("zorblax invoice")
    file = _file_with({"about_keys": frozenset({"e:zorblax", "e:invoice"})})
    verdict = open_verdict("ITEM1", file, concepts)
    assert verdict.in_index and verdict.exact and verdict.nearest() == ()
    assert tier_gap(verdict) == 0


def test_open_verdict_names_the_nearest_key_of_a_concept_not_held_exactly():
    concepts, _, _, _ = concepts_of("zorblax invoice")
    file = _file_with({"about_keys": frozenset({"e:invoice", "lw:zorblax"})})   # zorblax only as an automatic label
    verdict = open_verdict("ITEM1", file, concepts)
    assert not verdict.exact
    (near,) = verdict.nearest()
    assert (near.word, near.source_class, near.where, near.keys) == (
        "zorblax", SourceClass.AUTOMATIC_RECORD, "record", ("lw:zorblax",))
    assert tier_gap(verdict) == 2


def test_open_verdict_chunk_words_count_as_exact_and_a_skeleton_does_not():
    concepts, _, _, _ = concepts_of("zorblax")
    chunk = ("ITEM1", 1, 0)
    assert open_verdict("ITEM1", _file_with({"chunk_keys": {chunk: frozenset({"w:zorblax"})}}), concepts).exact
    skeleton = open_verdict("ITEM1", _file_with({"chunk_keys": {chunk: frozenset({"x:zorblaks"})}}), concepts)
    assert not skeleton.exact and skeleton.covers[0].source_class == SourceClass.TRANSLATION
    assert tier_gap(skeleton) == 1


def test_open_verdict_no_key_at_all_and_file_missing_from_the_index():
    concepts, _, _, _ = concepts_of("zorblax")
    none = open_verdict("ITEM1", _file_with({}), concepts)
    assert not none.exact and none.covers[0].source_class is None and tier_gap(none) == 3
    gone = open_verdict("ITEM9", None, concepts)
    assert (gone.item_id, gone.in_index, gone.exact) == ("ITEM9", False, False)


def test_render_report_lists_missed_words_and_unexact_opens():
    concepts, _, _, _ = concepts_of("zorblax")
    tallies = {}
    tally_word(tallies, concepts[0], STATUS_ABSENT)
    verdict = open_verdict("ITEM1", _file_with({"about_keys": frozenset({"lw:zorblax"})}), concepts)
    text = render_report(user_id="u", since="2026-10-14", until="", searches=2, queries_scored=1,
                         skipped_filter_only=1, tallies=tallies, opened=[OpenedCase("zorblax", verdict)])
    assert "| zorblax | 1 | 1 | 0 | 0 | 0 |" in text
    assert "`zorblax`: automatic_record via record (lw:zorblax)" in text
    assert "Opened files: 1; exact for every concept: 0" in text


# -- the I/O half, against a fake repository ---------------------------------------------------------------------------

def _repo(held_prefixes, record=None, chunk_keys=None):
    """A port fake: a stream answers non-empty when the asked keys carry one of `held_prefixes`."""
    repo = AsyncMock(spec=DriveIndexRepository)

    def answer(_user, keys, _limit, **_kw):
        return ["hit"] if any(k.startswith(p) for k in keys for p in held_prefixes) else []

    repo.find_by_about_keys.side_effect = answer
    repo.find_by_name_keys.side_effect = answer
    repo.find_chunks_by_word_keys.side_effect = answer
    repo.find_hints_by_keys.side_effect = answer
    repo.get_records.return_value = {record.item_id: record} if record else {}
    repo.get_chunk_keys.return_value = chunk_keys or {}
    repo.get_hint_keys.return_value = {}
    return repo


async def test_measure_counts_a_word_held_only_by_an_automatic_label():
    repo = _repo({"lw:"})
    searches = pair_searches(SAMPLE)
    result = await script.measure(repo, "u", searches)
    assert result["scored"] == 2 and result["skipped"] == 2
    t = result["tallies"]["quibbleton"]
    assert t.counts[STATUS_AUTOMATIC] == 1 and t.missed() == 1
    assert result["tallies"]["4417"].counts[STATUS_ABSENT] == 1     # `lw:` is not a number family


async def test_measure_reads_the_opened_file_and_classifies_it():
    record = DriveIndexRecord(user_id="u", connection_id="u", item_id="ITEM1", about_keys=("e:zorblax", "lw:invoice"),
                              indexed_generation=1, chunk_count=1)
    repo = _repo({"e:", "lw:"}, record=record, chunk_keys={("ITEM1", 1, 0): ("w:other",)})
    result = await script.measure(repo, "u", pair_searches(SAMPLE[:2]))
    (case,) = result["opened"]
    assert case.verdict.in_index and not case.verdict.exact
    assert {c.word: c.source_class for c in case.verdict.covers}["zorblax"] == SourceClass.VERIFIED_RECORD
    assert {c.word: c.source_class for c in case.verdict.covers}["invoice"] == SourceClass.AUTOMATIC_RECORD
    repo.get_chunk_keys.assert_awaited_once()


async def test_the_same_question_is_asked_of_the_index_once():
    repo = _repo(set())
    await script.measure(repo, "u", pair_searches([SAMPLE[2], SAMPLE[2]]))
    first = repo.find_by_about_keys.await_count
    repo.reset_mock()
    await script.measure(repo, "u", pair_searches([SAMPLE[2]]))
    assert first == repo.find_by_about_keys.await_count


def test_report_path_is_confined_to_scripts_memory(tmp_path):
    assert script.resolve_out(None, "u", "2026-10-14").name == "drive_query_vocabulary_2026-10-14.md"
    with pytest.raises(SystemExit):
        script.resolve_out(tmp_path / "x.md", "u", "2026-10-14")
    with pytest.raises(SystemExit):
        script.resolve_out(Path("scripts/memory/../x.md"), "u", "2026-10-14")
    assert script.resolve_out(Path("scripts/memory/a/x.md"), "u", "2026-10-14").name == "x.md"


def test_query_binds_the_user_and_window_without_interpolation():
    q = script.build_query("p.d.t", True)
    assert "@user" in q and "@since" in q and "@until" in q and "'u'" not in q
    assert "@until" not in script.build_query("p.d.t", False)
