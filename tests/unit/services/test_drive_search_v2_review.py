"""DriveSearchService, Search v2 — the review fixes of rv_sv2_p6 (USER_DRIVE_INDEXING_RFC §25.5, D29, D30): a full
page never yields a flat negative in scope (M1), validity queries filter the expiry date and time words are not
concepts (M2), no migration mode (M3), the name stream fetches per word (M4), and the minors."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_WAITING, describe_gap
from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_index import RecordState, Scope, DriveIndexRecord
from src.domain.drive_keys import name_keys_v2, query_concepts
from src.domain.drive_search import DriveSearchRequest, render_results, require_search_terms
from src.domain.drive_search_texts import REBUILDING_LINE
from src.services import drive_search_service as search_mod

from .drive_search_v2_fixture import V2World, card, tier_of

YEAR_2026 = dict(date_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 date_to=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))


def policy():
    return DriveProfile("Паспорт", "A passport", "ru", "id_document",
                        validity_words=("expires", "valid until", "истекает", "действителен до", "діє до"))


# -- M1 --------------------------------------------------------------------------------------------------------------

async def test_m1_a_full_word_page_under_a_folder_filter_never_claims_absence(monkeypatch):
    monkeypatch.setattr(search_mod, "WORD_FETCH", 2)
    monkeypatch.setattr(search_mod, "WORD_FILES_MAX", 2)
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 1)   # the folder is searched with user-wide streams
    w = V2World()
    for i in range(5):
        w.file(f"a{i}.md", kind="markdown", item=f"A{i}", chunks=["The boiler warranty."])
    work = w.folder("Work", item="ZF")
    w.folder("Sub", parent=work, item="ZG")
    w.file("z.md", kind="markdown", parent=work, item="ZZ", chunks=["The boiler warranty."])
    text = render_results(await w.search("boiler", folder="Work"))
    assert "No file in this scope holds" not in text
    assert 'could not be checked in the whole scope: "boiler"' in text


# -- M2 --------------------------------------------------------------------------------------------------------------

async def test_m2_a_validity_query_bounds_the_expiry_date_not_the_document_date():
    w = V2World()
    passport = w.file("passport.pdf", profile=policy(), valid_until="2026-12-09", effective="2016-04-01")
    w.file("insurance.pdf", profile=policy(), valid_until="2027-03-01", effective="2026-02-01")
    result = await w.search("истекает", **YEAR_2026)
    assert [c.item_id for c in result.cards if c.tier == "labelled"] == [passport]


async def test_m2_the_span_left_in_the_words_is_ignored_and_named():
    w = V2World()
    passport = w.file("passport.pdf", profile=policy(), valid_until="2026-12-09", effective="2016-04-01")
    result = await w.search("что истекает в этом году")
    assert tier_of(result, passport) == "labelled"
    text = render_results(result)
    assert '"году"' in text[text.index("ignored:"):] and 'No file holds' not in text


def test_m2_time_words_in_four_languages_are_not_concepts():
    for query in ("this year", "last year", "в этом году", "в прошлом году", "цього року", "минулого року",
                  "este año", "el año pasado", "за последний месяц"):
        q = query_concepts(query)
        assert q.concepts == (), (query, q.concepts)


def test_m2_the_date_fields_say_they_bound_the_expiry_date_for_what_expires():
    # Reviewer edit at the cut: the date fields are the specialist's `search_drive.date_from` / `date_to`, their texts
    # drive_tool_texts' (DRIVE_SPECIALIST_RFC §7.6, §8.1); the Smart-era constants are deleted
    from src.domain.drive_tool_texts import DATE_FROM, DATE_TO
    for text in (DATE_FROM, DATE_TO):
        assert "For what expires, it bounds the expiry date." in text


# the v1 `name_keys("Rome tickets.pdf")` (the function is retired, xc m4), literally
V1_ROME_TICKETS = (
    't:cke', 't:ets', 't:ick', 't:ket', 't:ome', 't:pdf', 't:rom', 't:tic', 't:икк', 't:кец', 't:кке', 't:оме', 't:пдф',
    't:ром', 't:тик', 'w:pdf', 'w:rome', 'w:tickets', 'w:пдф', 'w:роме', 'w:тиккец')


# -- M3 --------------------------------------------------------------------------------------------------------------

async def test_m3_a_state_not_rebuilt_reads_no_v1_record_and_says_it_is_being_rebuilt():
    w = V2World(migrated=False)
    v1 = DriveIndexRecord(user_id="u", connection_id="u", item_id="V1", parent_id="root", name="Rome tickets.pdf", kind="pdf",
                          scope=Scope.NORMAL, state=RecordState.INDEXED, has_card=True,
                          name_tokens=V1_ROME_TICKETS)   # a v1 record's name keys
    w.repo.records[("u", "V1")] = v1
    w.file("rome tickets v2.pdf")
    result = await w.search("rome tickets")
    # XC-1 (supersedes the §25.8 "searches the v2 records written so far"): a state not rebuilt runs no stream, so no
    # record is returned and no word is stated absent
    assert result.cards == ()
    text = render_results(result)
    assert REBUILDING_LINE in text and "migrated" not in text
    assert "No file holds" not in text and "No searched file holds" not in text


# -- M4 --------------------------------------------------------------------------------------------------------------

async def test_m4_every_word_of_a_long_query_reaches_the_name_stream():
    w = V2World()
    seen = []
    find = w.repo.find_by_name_keys

    async def spy(user_id, keys, limit, kind=None):
        seen.append(tuple(keys))
        return await find(user_id, keys, limit, kind)

    w.repo.find_by_name_keys = spy
    target = w.file("zanzibar.pdf")
    result = await w.search("медицинские записи холестерину Coimbra receipts tickets 2023 zanzibar")
    assert seen and max(len(keys) for keys in seen) <= 30
    assert any("w:zanzibar" in keys for keys in seen)
    assert 'No file holds "zanzibar"' not in render_results(result)
    assert target not in [c.item_id for c in result.cards if c.tier != "related"]   # strict AND: other words absent


# -- minors ----------------------------------------------------------------------------------------------------------

async def test_m1_minor_a_folder_named_by_digits_is_found_by_name():
    w = V2World()
    contracts = w.folder("Contracts")
    year = w.folder("2026", parent=contracts)
    w.file("lease.pdf", parent=year, chunks=["The lease of the flat."])
    result = await w.search("lease", folder="2026")
    assert result.status == "ok" and result.folder_path == "Contracts/2026"


async def test_m3_minor_a_labelled_card_with_a_grounded_keyword_shows_its_passage():
    w = V2World()
    report = w.file("analitica.pdf", profile=DriveProfile(
        "Análisis", "Blood test", "es", "medical_record", label_words=("медицинские записи",),
        topic_words=("холестерин",)),
        chunks=["Perfil lipídico. Colesterol total: 182 mg/dL (normal)."],
        hints={0: [("холестерин", "Colesterol total: 182 mg/dL")]})
    result = await w.search("медицинские записи по холестерину")
    assert tier_of(result, report) == "labelled" and "Colesterol total" in card(result, report).passage


async def test_m4_minor_a_cyrillic_misspelling_of_a_latin_name_is_a_similar_name():
    w = V2World()
    item = w.file("Coimbra.jpg", kind="image", taken="2025-01-01")
    result = await w.search("коїмба")
    assert card(result, item).similar_name


async def test_m6_minor_a_file_named_by_a_generic_word_is_found_by_name():
    w = V2World()
    todo = w.file("todo.md", kind="markdown", chunks=["Buy milk."])
    request = DriveSearchRequest("todo")
    require_search_terms(request)   # not refused: there are name words
    result = await w.service.search("u", request)
    assert [c.item_id for c in result.cards] == [todo]


def test_m7_minor_a_number_is_never_missable_as_common():
    from src.domain.drive_tiers import allowances
    cs = query_concepts("receipts 2024").concepts
    assert allowances(cs, {"receipts": 3, "2024": 90}, carded=100).common == frozenset()


async def test_m9_minor_a_sheet_waiting_for_its_profile_is_not_counted_as_without_passages():
    w = V2World()
    item = w.file("budget.xlsx", kind="xlsx", profile_state="ready")
    # XC-3/XC-9: the tick derives the gap and stores its count in the state; search reads only that counter
    w.set(item, describe_gap=describe_gap(w.repo.records[("u", item)]))
    assert w.repo.records[("u", item)].describe_gap == GAP_WAITING
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, counters={**state.counters, COVERAGE_COUNTERS[GAP_WAITING]: 1})
    text = render_results(await w.search("zebra"))
    assert "no passage keywords" not in text and "1 text file is not described yet" in text


async def test_m10_minor_the_about_heading_keeps_the_query_order():
    w = V2World()
    w.file("IMG_1.jpg", kind="image", taken="2023-06-01", place_forms=("Berlin",))
    assert 'About "berlin 2023"' in render_results(await w.search("Berlin 2023"))


def test_m10_minor_one_group_member_is_singular():
    from src.domain.drive_match import MatchTier
    from src.domain.drive_tier_render import group_lines
    from src.domain.drive_tier_result import GroupMember
    assert group_lines([GroupMember("A", "pdf", "2025-01")], MatchTier.ABOUT) == (
        "A: 1 more matching file (PDF, 2025-01)",)


def test_m12_minor_v1_search_code_is_gone():
    import importlib.util
    import src.domain.drive_search as ds
    import src.domain.drive_index_params as params
    assert importlib.util.find_spec("src.domain.drive_content_search") is None
    for name in ("rank_names", "cover_first", "MATCHED_CARD", "MATCHED_CONTENT", "MATCHED_BOTH"):
        assert not hasattr(ds, name), name
    card_fields = ds.DriveSearchCard.__dataclass_fields__
    assert not {"matched", "name_matched", "window", "passage_exact", "window_shortened"} & set(card_fields)
    assert not {"capped", "candidates"} & set(ds.DriveSearchResult.__dataclass_fields__)
    for name in ("CONTENT_TERM_FETCH", "TERM_NONSELECTIVE_FILES", "RRF_K"):
        assert not hasattr(params, name), name


def test_name_keys_v2_still_keys_digit_folders():
    assert "n:2026" in name_keys_v2("2026")


def test_m10_minor_the_cabinet_names_which_provider_reads_scans():
    from tests.unit.web.test_cabinet_drive_index_strings import _dicts
    _, dicts = _dicts()
    wanted = {"en": "read in full by that provider", "uk": "цей постачальник читає повністю",
              "fr": "lus en entier par ce fournisseur", "es": "los lee enteros ese proveedor"}
    for lang, phrase in wanted.items():
        assert phrase in dicts[lang]["drive.index_disclosure"], lang
