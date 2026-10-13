"""DriveSearchService after the Search v2 cross-cutting review (USER_DRIVE_INDEXING_RFC §25.5, §25.8): a state not
rebuilt under v2 is not searched (XC-1), the coverage counts come from the stored counters (XC-3, XC-9), the Cabinet
status (XC-4, XC-8), and F1, F2, F5 and D31 end to end on the real service."""
from dataclasses import replace

import pytest

from src.domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_HELD, GAP_NO_PASSAGES, GAP_WAITING
from src.domain.drive_enrichment import DriveProfile, ProfileEntity
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_planner import month_key
from src.domain.drive_keys import name_keys_v2
from src.domain.drive_search import render_results
from src.domain.drive_search_texts import REBUILDING_LINE

from .drive_search_v2_fixture import V2World, card, tier_of


class CountingRepo:
    """Wraps a world's repository: counts the reads a search makes."""

    def __init__(self, w: V2World) -> None:
        self.calls = []
        repo = w.repo
        for name in ("count_by_job_state", "count_describe_gaps", "find_by_about_keys", "find_chunks_by_word_keys",
                     "find_hints_by_keys", "find_by_name_keys", "filter_records", "find_nearest_cards",
                     "find_nearest_chunks", "find_nearest_hints", "get_records"):
            original = getattr(repo, name)

            async def spy(*a, _name=name, _original=original, **kw):
                self.calls.append(_name)
                return await _original(*a, **kw)

            setattr(repo, name, spy)


def _v1_record(w: V2World) -> None:
    w.repo.records[("u", "V1")] = DriveIndexRecord(
        user_id="u", item_id="V1", parent_id="root", name="Rome tickets.pdf", kind="pdf", scope=Scope.NORMAL,
        state=RecordState.INDEXED, has_card=True, name_tokens=name_keys_v2("Rome tickets.pdf"), keys_version=1)


# -- XC-1 ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [dict(query="rome tickets"), dict(kind="pdf"), dict(query="files")])
async def test_an_index_awaiting_its_rebuild_runs_no_stream_and_states_nothing(kw):
    w = V2World(migrated=False)
    _v1_record(w)
    spy = CountingRepo(w)
    result = await w.search(**kw)
    assert result.awaiting_rebuild and result.cards == () and result.tiered is None
    assert spy.calls == [] and w.emb.calls == []   # no read, no query embedding
    text = render_results(result)
    assert REBUILDING_LINE in text and "No file holds" not in text and "Rome tickets" not in text


async def test_a_rebuilt_index_is_searched():
    w = V2World()
    item = w.file("Rome tickets.pdf")
    result = await w.search("rome tickets")
    assert not result.awaiting_rebuild and tier_of(result, item) == "about"


# -- XC-3 / XC-9 -----------------------------------------------------------------------------------------------------

async def test_the_coverage_counts_are_read_from_the_stored_counters_with_no_count_query():
    w = V2World()
    w.file("Rome tickets.pdf")
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, counters={**state.counters, COVERAGE_COUNTERS[GAP_WAITING]: 3,
                                                  COVERAGE_COUNTERS[GAP_HELD]: 1,
                                                  COVERAGE_COUNTERS[GAP_NO_PASSAGES]: 2})
    spy = CountingRepo(w)
    result = await w.search("zebra")
    cov = result.tiered.coverage
    assert (cov.not_described, cov.not_describable, cov.no_passages) == (3, 1, 2)
    assert "count_by_job_state" not in spy.calls and "count_describe_gaps" not in spy.calls


async def test_no_stored_counts_is_complete():
    w = V2World()
    w.file("Rome tickets.pdf", profile=DriveProfile("t", "w", "en", "ticket"), profile_state="deferred",
           passages_state="deferred")
    result = await w.search("rome")
    assert result.tiered.coverage.not_described == 0
    assert "Coverage: complete." in render_results(result)


# -- XC-4 / XC-8: the Cabinet status ---------------------------------------------------------------------------------

async def test_the_status_shows_the_month_caps_dollars_without_the_initial_builds():
    w = V2World()
    now = w.now
    await w.repo.add_model_usage("u", month_key(now), now.date().isoformat(), "profile", 12.5, 0.0, 9,
                                 covered_usd=12.0)
    status = await w.service.indexing_status("u")
    assert status.usd_month["profile"] == pytest.approx(0.5)


async def test_the_status_says_a_v1_index_awaits_its_rebuild():
    gated, rebuilt = V2World(migrated=False), V2World()
    assert (await gated.service.indexing_status("u")).awaiting_rebuild
    assert not (await rebuilt.service.indexing_status("u")).awaiting_rebuild


async def test_a_purge_in_progress_is_not_awaiting_a_rebuild():
    w = V2World(migrated=False)
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True)
    assert not (await w.service.indexing_status("u")).awaiting_rebuild


# -- F1 end to end ---------------------------------------------------------------------------------------------------

async def test_a_form_the_file_does_not_print_never_puts_it_in_about():
    w = V2World()
    real = w.file("Паспорт скан.pdf")
    letter = w.file("letter.pdf", profile=DriveProfile("Letter", "A letter", "en", "letter", entities=(
        ProfileEntity("паспорт", "person", "Smith", forms=("паспорт", "passport")),)))
    result = await w.search("паспорт")
    assert tier_of(result, real) == "about" and tier_of(result, letter) == "translated"
    assert 'person "паспорт" (spelling by the index; named in the file as "Smith")' in card(result, letter).sources


async def test_the_card_shows_the_printed_spelling():
    w = V2World()
    item = w.file("pass.pdf", profile=DriveProfile("Boarding pass", "A boarding pass", "en", "ticket", entities=(
        ProfileEntity("Барселона", "place", "BARCELONA", forms=("Barcelona",), found=("Barcelona",)),)))
    result = await w.search("barcelona")
    assert tier_of(result, item) == "about"
    assert "BARCELONA" in card(result, item).summary and "Барселона" not in card(result, item).summary


# -- F2 end to end ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,query", [("Mañana.docx", "mañana"), ("My.docx", "my"), ("Today.md", "today"),
                                        ("The The - live.mp3", "the the")])
async def test_a_query_of_removed_words_finds_the_name(name, query):
    w = V2World()
    item = w.file(name, kind="docx")
    w.file("Other.pdf")
    result = await w.search(query)
    assert tier_of(result, item) == "about"
    assert "No name words or filters were given" not in render_results(result)


# -- F3 end to end ---------------------------------------------------------------------------------------------------

async def test_mario_does_not_find_maria():
    w = V2World()
    maria = w.file("Maria.jpg", kind="image")
    result = await w.search("Mario")
    assert tier_of(result, maria) in (None, "related")


# -- F5 end to end ---------------------------------------------------------------------------------------------------

async def test_what_expires_shows_the_expiry_date():
    w = V2World()
    profile = DriveProfile("Passport", "A passport", "en", "id_document", validity_words=("expires", "истекает"))
    item = w.file("passport scan.pdf", profile=profile, valid_until="2026-11-15", effective="2016-11-15")
    result = await w.search("истекает")
    c = card(result, item)
    assert c.expiry_match and "date: 2026-11-15 (expires)" in render_results(result)


async def test_a_name_match_on_a_valid_file_shows_its_validity_line():
    w = V2World()
    profile = DriveProfile("Passport", "A passport", "en", "id_document", validity_words=("expires",))
    item = w.file("passport scan.pdf", profile=profile, valid_until="2026-11-15", effective="2016-11-15")
    result = await w.search("scan")
    assert not card(result, item).expiry_match and "valid until: 2026-11-15" in render_results(result)


# -- D31 end to end --------------------------------------------------------------------------------------------------

async def test_medical_records_about_cholesterol_reach_the_labelled_tier_from_the_table():
    """The lab report's profile wrote no label words at all: the D31 names of `medical_record` cover "медицинские"
    and "записи", its topic words "холестерину"."""
    w = V2World()
    report = w.file("analisis.pdf", profile=DriveProfile(
        "Análisis de sangre", "Blood test results", "es", "medical_record", life_domains=("health",),
        topic_words=("cholesterol", "colesterol", "холестерин")), chunks=["Colesterol total 210 mg/dL."])
    for i in range(3):
        w.file(f"other{i}.pdf")
    result = await w.search("медицинские записи по холестерину")
    assert tier_of(result, report) in ("labelled", "translated")


# -- the number allowance end to end --------------------------------------------------------------------------------

async def test_a_bill_of_another_year_does_not_match_the_year():
    w = V2World()
    old = w.file("electricity bill.pdf", effective="2026-03-01",
                 profile=DriveProfile("Bill", "Electricity bill", "en", "bill"))
    for i in range(4):
        w.file(f"report {i}.pdf", effective=f"2024-0{i + 1}-01")
    result = await w.search("electricity bill 2024")
    assert tier_of(result, old) in (None, "related")


