"""DriveSearchService, Search v2 (USER_DRIVE_INDEXING_RFC §25.5): the §25.5.1 walk-throughs end to end over a synthetic
enrichment fixture, strict AND with the D29 count line, the coverage and migration lines, filters, fencing, folder
scope and the log line."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_enrich_plan import COVERAGE_COUNTERS, GAP_HELD, GAP_WAITING, derived
from src.domain.drive_enrichment import DriveProfile, JobState, ProfileEntity
from src.domain.drive_index_params import ABOUT_SHOWN, CONCEPT_KEYS_MAX, RELATED_MAX
from src.domain.drive_search import render_results
from src.domain.drive_search_texts import FOOTER_EXACT

from .drive_index_fakes import U_SCOPE
from .drive_search_v2_fixture import V2World, card, tier_of, vec


@pytest.fixture
def w():
    return V2World()


def lab_report():
    return DriveProfile(
        title="Análisis de sangre", what_it_is="Blood test results with a lipid panel", language="es",
        doc_type="medical_record", life_domains=("health",),
        topics=("cholesterol",), topics_native=("colesterol",),
        topic_words=("cholesterol", "colesterol", "холестерин", "холестерол"),
        label_words=("medical record", "medical records", "historia clínica", "медична картка",
                     "медицинская запись", "медицинские записи"),
        domain_words=("health", "salud", "здоровье", "здоров'я"))


# -- §25.5.1 walk-throughs ------------------------------------------------------------------------------------------

class TestCoimbra:
    async def test_about_mentioned_and_by_translation(self, w):
        photo = w.file("IMG_0001.jpg", kind="image", taken="2025-05-03",
                       place_forms=("Coimbra", "Коїмбра", "Coïmbre", "Portugal", "Португалія"))
        doc = w.file("Договір.pdf", profile=DriveProfile("Договір оренди", "Lease", "uk", "contract", entities=(
            ProfileEntity("Коїмбра", "place", "Коїмбра"),)))
        note = w.file("нотатки.md", kind="markdown", chunks=["Влітку ми були в Коїмбрі й багато гуляли."])
        latin = w.file("notes.md", kind="markdown", chunks=["Notes from the Coimbra trip, day two."])
        grounded = w.file("viaje.pdf", chunks=["Llegamos a la ciudad de los estudiantes el martes por la tarde."],
                          hints={0: [("Коїмбра", "la ciudad de los estudiantes")]})
        result = await w.search("про Коїмбру")
        assert [tier_of(result, i) for i in (photo, doc, note, latin, grounded)] == [
            "about", "about", "mentioned", "translated", "translated"]
        assert card(result, photo).sources == ('place "Коїмбра" (photo location)',)
        assert card(result, doc).sources == ('place "Коїмбра" (named in the file)',)
        assert card(result, grounded).sources == ('passage keyword "Коїмбра", from "la ciudad de los estudiantes"',)
        assert "la ciudad de los estudiantes" in card(result, grounded).passage
        text = render_results(result)
        assert '"коїмбру"; ignored: "про".' in text and FOOTER_EXACT in text


class TestCoimbraTrip:
    async def test_a_note_holding_both_words_is_mentioned(self, w):
        note = w.file("plan.md", kind="markdown", chunks=["Coimbra trip: book the hotel near the university."])
        other = w.file("photo.jpg", kind="image", place_forms=("Coimbra", "Коїмбра"), taken="2025-05-01")
        result = await w.search("Coimbra trip")
        assert tier_of(result, note) == "mentioned" and tier_of(result, other) is None

    async def test_an_empty_conjunction_names_each_words_files(self, w):
        w.file("photo.jpg", kind="image", place_forms=("Coimbra",), taken="2025-05-01")
        w.file("photo2.jpg", kind="image", place_forms=("Coimbra",), taken="2025-05-02")
        w.file("trip.md", kind="markdown", chunks=["A trip to the mountains."])
        result = await w.search("Coimbra trip")
        assert [c.tier for c in result.cards if c.tier != "related"] == []
        assert 'No file matches all of: "coimbra" (2 files), "trip" (1 file).' in render_results(result)


class TestBerlin2023:
    async def test_photo_by_place_and_year_in_two_scripts(self, w):
        berlin = w.file("IMG_2210.jpg", kind="image", taken="2023-06-01", place_forms=("Berlin", "Берлін", "Germany"))
        w.file("IMG_9.jpg", kind="image", taken="2024-06-01", place_forms=("Berlin", "Берлін", "Germany"))
        for query in ("Berlin 2023", "Берлін 2023"):
            result = await w.search(query)
            assert [c.item_id for c in result.cards if c.tier == "about"] == [berlin], query


class TestAllMyReceipts:
    async def test_label_name_and_mention(self, w):
        receipt = DriveProfile("Ticket de compra", "A shop receipt", "es", "receipt",
                               label_words=("receipt", "receipts", "recibo", "recibos", "квитанция", "квитанции"))
        labelled = w.file("scan_0042.pdf", profile=receipt)
        named = w.file("Receipt 2024-03.pdf")
        mentioned = w.file("todo.md", kind="markdown", chunks=["Keep the receipt for the warranty claim."])
        result = await w.search("all my receipts")
        assert {tier_of(result, i) for i in (labelled, named, mentioned)} == {"labelled", "about", "mentioned"}
        assert tier_of(result, labelled) == "labelled" and tier_of(result, named) == "about"
        assert card(result, labelled).sources == ('type label "receipts" (automatic)',)
        assert '; ignored: "all", "my".' in render_results(result)
        russian = await w.search("квитанции")
        assert tier_of(russian, labelled) == "labelled"


class TestCholesterol:
    async def test_spanish_lab_report_via_label_topic_and_a_grounded_hint(self, w):
        report = w.file("analitica_2025.pdf", profile=lab_report(),
                        chunks=["Perfil lipídico. Colesterol total: 182 mg/dL (normal). HDL: 58 mg/dL."],
                        hints={0: [("холестерин", "Colesterol total: 182 mg/dL")]})
        w.file("recipe.md", kind="markdown", chunks=["A lentil soup with carrots."])
        result = await w.search("медицинские записи по холестерину")
        assert tier_of(result, report) == "labelled"
        sources = card(result, report).sources
        assert 'type label "медицинские" (automatic)' in sources[0]
        assert any(s.startswith('passage keyword "холестерин", from "Colesterol total') for s in sources)

    async def test_d29_when_one_word_is_held_by_no_file(self, w):
        w.file("analitica_2025.pdf", profile=lab_report())
        result = await w.search("медицинские справки по холестерину")
        assert [c for c in result.cards if c.tier != "related"] == []
        assert render_results(result).endswith(
            'No file holds "справки".\nWithout "справки": 1 file matches the other words.')


class TestWhatExpires:
    async def test_validity_words_soonest_first_and_expired_counted(self, w):
        def policy(until):
            return DriveProfile("Póliza", "Car insurance policy", "es", "insurance_policy",
                                validity_words=("expires", "valid until", "caduca", "истекает"))
        late = w.file("insurance.pdf", profile=policy(None), valid_until="2027-03-01")
        soon = w.file("passport.pdf", profile=policy(None), valid_until="2026-12-09")
        gone = w.file("old.pdf", profile=policy(None), valid_until="2025-01-01")
        result = await w.search("what expires")
        labelled = [c.item_id for c in result.cards if c.tier == "labelled"]
        assert labelled == [soon, late] and gone not in labelled
        assert "1 more file with these words already expired." in render_results(result)


# -- strict AND, allowances and absent words ------------------------------------------------------------------------

class TestStrictAnd:
    async def test_an_unmatched_word_is_never_dropped(self, w):
        w.file("rome.md", kind="markdown", chunks=["Rome in spring."])
        w.file("vaccination.md", kind="markdown", chunks=["Vaccination record of the dog."])
        result = await w.search("rome vaccination")
        assert [c for c in result.cards if c.tier != "related"] == []

    async def test_absent_word_outside_the_filter_says_in_this_scope(self, w):
        w.file("rome.md", kind="markdown", chunks=["Rome tickets."])
        result = await w.search("rome", kind="pdf")
        assert 'No file in this scope holds "rome".' in render_results(result)

    async def test_low_weight_word_may_be_missed_with_three_words(self, w):
        for i in range(6):
            w.file(f"t{i}.md", kind="markdown", chunks=[f"Tickets bought, number {i}."])
        for i in range(20):   # tickets stays under COMMON_SHARE of the carded files
            w.file(f"other{i}.pdf")
        target = w.file("colosseum.md", kind="markdown", chunks=["Rome: the Colosseum at dawn."])
        result = await w.search("rome colosseum tickets")
        assert tier_of(result, target) == "mentioned"
        assert card(result, target).not_matched == ('not matched: "tickets"',)


# -- coverage, migration, filters, fence, scope ---------------------------------------------------------------------

class TestCoverage:
    async def test_complete(self, w):
        w.file("rome.md", kind="markdown", chunks=["Rome."])
        assert "Coverage: complete." in render_results(await w.search("rome"))

    async def test_not_described_counts_text_files_never_folders_or_photos(self, w):
        w.file("a.pdf", profile_state=JobState.READY.value)
        w.file("b.pdf", profile_state=JobState.HELD.value)
        w.file("c.jpg", kind="image", taken="2025-01-01")
        folder = w.folder("Repo")
        w.set(folder, profile_state=JobState.READY.value)
        # XC-3/XC-9: the planner derives `describe_gap` per record, the tick counts it (`count_describe_gaps`) into the
        # state's coverage counters, and search reads only those
        for (user, item), rec in list(w.repo.records.items()):
            w.repo.records[(user, item)] = derived(rec, datetime(2026, 10, 20, tzinfo=timezone.utc))
        gaps = await w.repo.count_describe_gaps(U_SCOPE)
        assert gaps == {GAP_WAITING: 1, GAP_HELD: 1}                # not the folder, not the photo
        state = w.repo.states["u"]
        w.repo.states["u"] = replace(state, counters={
            **state.counters, **{COVERAGE_COUNTERS[g]: n for g, n in gaps.items()}})
        text = render_results(await w.search("zebra"))
        assert ("Coverage: incomplete — 1 text file is not described yet; 1 text file could not be described."
                in text)


class TestFilters:
    async def test_document_family_includes_markdown_and_text(self, w):
        md = w.file("rome.md", kind="markdown", chunks=["Rome."])
        photo = w.file("rome.jpg", kind="image", taken="2025-01-01", place_forms=("Rome",))
        result = await w.search("rome", kind="document")
        assert tier_of(result, md) == "about" and tier_of(result, photo) is None   # its name holds the word

    async def test_date_filter(self, w):
        old = w.file("rome-2019.md", kind="markdown", chunks=["Rome."], effective="2019-05-01")
        new = w.file("rome-2025.md", kind="markdown", chunks=["Rome."], effective="2025-05-01")
        from datetime import datetime, timezone
        result = await w.search("rome", date_from=datetime(2024, 1, 1, tzinfo=timezone.utc))
        assert tier_of(result, new) == "about" and tier_of(result, old) is None


class TestFence:
    async def test_a_chunk_of_another_generation_is_not_a_hit(self, w):
        item = w.file("rome.md", kind="markdown", chunks=["Rome in spring."], generation=1)
        w.set(item, indexed_generation=2)
        result = await w.search("spring")
        assert tier_of(result, item) not in ("about", "mentioned", "translated", "labelled")
        assert 'No file holds "spring".' in render_results(result)


class TestFolderScope:
    async def test_in_memory_subtree(self, w):
        travel = w.folder("Travel")
        inside = w.file("rome.md", kind="markdown", parent=travel, chunks=["Rome in spring."])
        outside = w.file("rome2.md", kind="markdown", chunks=["Rome in spring."])
        result = await w.search("spring", folder="Travel")
        assert tier_of(result, inside) == "mentioned" and tier_of(result, outside) is None
        assert "Searched only within the requested folder" in render_results(result)


class TestShownCounts:
    async def test_about_shows_ten_and_counts_the_rest(self, w):
        for i in range(ABOUT_SHOWN + 3):
            w.file(f"IMG_{i:04d}.jpg", kind="image", taken=f"2025-05-{i + 1:02d}", place_forms=("Coimbra",))
        result = await w.search("Coimbra")
        assert sum(1 for c in result.cards if c.tier == "about") == ABOUT_SHOWN
        text = render_results(result)
        assert f'About "coimbra" (showing {ABOUT_SHOWN} of {ABOUT_SHOWN + 3}):' in text
        assert "/: 3 more matching files (image, 2025-05)" in text


class TestRelated:
    async def test_at_most_three_by_meaning_never_counted(self, w):
        target, other = vec("target"), vec("other")
        for i in range(30):   # a tight cloud of unrelated cards: the pool's median and spread
            w.file(f"misc{i}.pdf", card_vector=[x + 0.02 * ((i * 7 + j) % 5) for j, x in enumerate(other)])
        near = [w.file(f"near{i}.pdf", card_vector=[x + 0.001 * i for x in target]) for i in range(5)]
        w.emb.pinned["sunset"] = target
        result = await w.search("sunset")
        related = [c.item_id for c in result.cards if c.tier == "related"]
        assert 0 < len(related) <= RELATED_MAX and set(related) <= set(near)
        assert "Related by meaning only, unverified" in render_results(result)

    async def test_small_drive_closest_by_meaning(self, w):
        w.file("a.pdf")
        w.file("b.pdf")
        result = await w.search("zebra")
        text = render_results(result)
        assert "closest by meaning; the drive is too small" in text
        assert 'No file holds "zebra".' in text

    async def test_similar_name(self, w):
        item = w.file("Coimbra.pdf")
        result = await w.search("Coimrba")   # misspelled: no file holds it
        assert card(result, item).similar_name and card(result, item).tier == "related"
        assert 'No file holds "coimrba".' in render_results(result)


class TestKeysAndLog:
    async def test_every_fetch_takes_at_most_thirty_keys(self, w):
        w.file("x.md", kind="markdown", chunks=["Медицинские записи по холестерину."])
        await w.search("медицинские записи по холестерину Coimbra receipts tickets 2023")
        assert w.repo.key_lists and max(n for _, n in w.repo.key_lists) <= CONCEPT_KEYS_MAX

    async def test_log_line_has_the_v2_stats(self, w, caplog):
        import logging
        w.file("rome.md", kind="markdown", chunks=["Rome."])
        from src.utils.logger import logger as app_logger
        lines = []
        handler = logging.Handler()
        handler.emit = lambda record: lines.append(record.getMessage())
        app_logger.addHandler(handler)
        try:
            await w.search("rome tickets")
        finally:
            app_logger.removeHandler(handler)
        line = next(m for m in lines if m.startswith("[DriveSearch]"))
        for field in ("concepts=2", "ignored=", "common=", "absent=", "about=", "mentioned=", "translated=",
                      "labelled=", "related=", "pool_card=", "band=0", "coverage=", "chars="):
            assert field in line, field
        assert "term_" not in line and "fused=" not in line and "covered=" not in line


class TestFilterOnly:
    async def test_only_generic_words_is_a_filter_only_listing(self, w):
        from src.domain.drive_search import DriveSearchRequest
        assert DriveSearchRequest("all my documents").filter_only
        assert not DriveSearchRequest("rome").filter_only
        doc = w.file("a.pdf")
        result = await w.search("all my documents", kind="pdf")
        assert result.tiered is None and [c.item_id for c in result.cards] == [doc]
