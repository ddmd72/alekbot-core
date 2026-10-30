"""Review fixes of card accuracy (USER_DRIVE_INDEXING_RFC §26.4, Δ59-Δ61, Δ85): maximal prose terms, no separator-free
match of an amount or a date, an unconfirmed brief date moves nothing, a stale score cleared, digital-by-a-model
priced, skeletons across scripts only, two-digit years and bilingual months, the tables digest covering the spaced-date
pattern."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_accuracy as acc
from src.domain import drive_enrich_plan as enrich
from src.domain import drive_profile as dp
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_grade import TextEvidence, lenient_date
from src.domain.drive_index import (
    ACCURACY_VERSION, DocumentBrief, DriveChunk, DriveIndexRecord, RecordState, Scope, TriageVerdict,
)
from src.domain.drive_keys import about_keys_of, index_tables_digest
from src.domain.drive_search import date_source, make_card

IN, AS, NOT = Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value, Grade.NOT_IN_TEXT.value
NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def chunks(text):
    return [DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=0, text=text, char_start=0, char_end=len(text),
                       kind="pdf")]


def profile(**kw):
    base = dict(title="t", what_it_is="w", language="en", doc_type="other")
    base.update(kw)
    return DriveProfile(**base)


class TestProseTermsAreMaximal:
    def test_an_echoed_id_amount_and_date_add_no_fragment_grades(self):
        text = "Invoice INV-2024/0012 total 1 250,50 EUR dated 07.05.2031."
        p = profile(title="Invoice INV-2024/0012", what_it_is="Total 1 250,50 EUR dated 2031-05-07",
                    key_values=(KeyValue("invoice_number", "INV-2024/0012", "inv20240012"),
                                KeyValue("amount_total", "1 250,50", "1250.50")),
                    dates=(ProfileDate("issued", date(2031, 5, 7), "07.05.2031"),))
        graded, _ = dp.grade(p, chunks(text), "inv.pdf")
        numbers = {g.value for g in graded.graded if g.field == GradedField.NUMBER.value}
        assert not numbers & {"0012", "2024", "20240012", "1250", "250", "2031", "1250.50", "inv20240012"}


class TestNoSeparatorFreeMatchForAmountsAndDates:
    def test_an_amount_is_not_found_in_a_digit_run(self):
        ev = TextEvidence(["Reference 125000 only."], "")
        grade, canonical, seen = ev.value_grade("1.250,00 €", "1250.00")
        assert grade is Grade.NOT_IN_TEXT and canonical == "1250.00" and seen == ""

    def test_a_date_is_not_found_separator_free(self):
        ev = TextEvidence(["Code 07052031 here."], "")
        assert ev.value_grade("07.05.2031", "")[0] is Grade.NOT_IN_TEXT

    def test_an_id_with_separators_still_is(self):
        ev = TextEvidence(["Card X 1234567 A issued."], "")
        assert ev.value_grade("X1234567A", "")[0] is Grade.IN_TEXT_AS

    def test_a_prose_amount_term_is_not_rescued_either(self):
        assert TextEvidence(["Reference 125000 only."], "").term_grade("1250.00")[0] is Grade.NOT_IN_TEXT


class TestUnconfirmedBriefDate:
    BRIEF = DocumentBrief(doc_date=datetime(2040, 1, 1, tzinfo=timezone.utc), description="d")

    def rec(self, **kw):
        base = dict(user_id="u", item_id="i", name="f.pdf", kind="pdf", scope=Scope.NORMAL, state=RecordState.INDEXED,
                    indexed_generation=1, brief=self.BRIEF, effective_date=self.BRIEF.doc_date,
                    modified_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        base.update(kw)
        return DriveIndexRecord(**base)

    def test_the_grade_is_stored_on_the_brief(self):
        assert dp.brief_date_grade(self.BRIEF, chunks("nothing about it"), "f.pdf") == NOT
        assert dp.brief_date_grade(self.BRIEF, chunks("dated 01.01.2040"), "f.pdf") == IN
        assert dp.brief_date_grade(DocumentBrief(description="d"), chunks("x"), "f.pdf") == ""

    def test_an_unconfirmed_date_is_not_the_effective_date(self):
        out = enrich.brief_graded(self.rec(), chunks("nothing about it"), NOW)
        assert out.brief.date_grade == NOT and out.brief.held_date is None
        assert out.effective_date != self.BRIEF.doc_date

    def test_a_confirmed_date_stays_the_effective_date(self):
        out = enrich.brief_graded(self.rec(), chunks("dated 01.01.2040"), NOW)
        assert out.effective_date == self.BRIEF.doc_date

    def test_it_is_not_keyed_as_verified_nor_shown_as_the_document(self):
        out = enrich.brief_graded(self.rec(), chunks("nothing about it"), NOW)
        keys = set(about_keys_of(out))
        assert "y:2040" not in keys and "m:2040-01" not in keys
        assert date_source(out) != "document"

    def test_the_effective_date_of_a_later_observation_keeps_ignoring_it(self):
        out = enrich.brief_graded(self.rec(), chunks("nothing about it"), NOW)
        assert enrich.effective_from(out, None, now=NOW) != self.BRIEF.doc_date

    def test_an_ungraded_brief_date_counts(self):
        assert self.BRIEF.held_date == self.BRIEF.doc_date


class TestStaleScore:
    def test_clearing_the_profile_clears_the_score(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="pdf", indexed_generation=1, profile=profile(),
                               profile_source="model", card_accuracy=80, accuracy_version=ACCURACY_VERSION)
        out = enrich._profile_cleared(rec, NOW)
        assert out.card_accuracy is None and out.accuracy_version == 0

    def test_make_card_shows_the_score_only_of_a_scorable_record(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="image", card_accuracy=80,
                               triage_verdict=TriageVerdict.PHOTO, indexed_generation=None)
        assert make_card(rec, "/a", "s", "abcdefgh").card_accuracy is None
        scorable_rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="pdf", card_accuracy=80, indexed_generation=1,
                                        profile=profile(), profile_source="model")
        assert make_card(scorable_rec, "/a", "s", "abcdefgh").card_accuracy == 80


class TestRescoreWithIncompleteChunks:
    def test_a_brief_only_card_keeps_its_score_and_takes_the_version(self):
        from src.services.drive_indexing_service import DriveIndexingService
        svc = object.__new__(DriveIndexingService)
        svc._repo = AsyncMock()
        svc._repo.get_chunks.return_value = []            # none of the 2 chunks can be read
        svc._commit = AsyncMock()
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", name="f.pdf", kind="pdf", indexed_generation=1, chunk_count=2,
                               brief=DocumentBrief(amount="9.99 EUR"), card_accuracy=61, accuracy_version=0)
        summary = {}
        done = asyncio.run(svc._rescore("u", rec, NOW, summary))
        assert done.card_accuracy == 61 and done.accuracy_version == ACCURACY_VERSION
        assert summary["rescore_incomplete"] == 1
        svc._commit.assert_awaited_once()


class TestDigitalByAModel:
    def rec(self, **kw):
        return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="pdf", **kw)

    def test_a_model_read_digital_file_is_priced_like_a_screenshot(self):
        assert acc.source_share(self.rec(capture="digital", extracted_by="reader@2")) == 0.03

    def test_a_parser_read_digital_file_is_native(self):
        assert acc.source_share(self.rec(capture="digital", extracted_by="parser@3")) == 0.0
        assert acc.source_share(self.rec(capture="digital", capture_source="detected")) == 0.0

    def test_the_table_has_the_row(self):
        assert acc.SOURCE_SHARE["digital_model_read"] == 0.03


class TestSkeletonsCrossScriptsOnly:
    def test_two_latin_names_sharing_a_stem_are_not_a_transliteration(self):
        p, _ = dp.grade(profile(entities=(ProfileEntity("Maria", "person", "Maria"),)), chunks("Signed by Mario Rossi."),
                        "f.pdf")
        assert [g.grade for g in p.graded if g.field == "entity"] == [NOT]

    def test_a_cyrillic_spelling_of_a_latin_name_still_is(self):
        p, _ = dp.grade(profile(entities=(ProfileEntity("Maria", "person", "Maria"),)), chunks("Підпис: Марія Доу."),
                        "f.pdf")
        assert [g.grade for g in p.graded if g.field == "entity"] == [AS]


class TestLenientDate:
    def test_a_two_digit_year_against_the_profile_year(self):
        assert lenient_date("07.05.31", date(2031, 5, 7)) == date(2031, 5, 7)
        assert lenient_date("07.05.31", date(2131, 5, 7)) == date(2131, 5, 7)     # no century pivot
        assert lenient_date("07.05.31", date(2030, 5, 7)) is None                 # a different year is refused
        assert lenient_date("07.05.31") is None

    def test_a_two_digit_year_still_refuses_a_different_day(self):
        text = "Valid 07.05.31"
        p, _ = dp.grade(profile(dates=(ProfileDate("valid_until", date(2031, 5, 8), "07.05.31"),)), chunks(text), "f.pdf")
        assert [g.grade for g in p.graded] == [NOT]

    def test_a_two_digit_year_is_in_text_as_when_it_reads_to_the_profile_date(self):
        p, _ = dp.grade(profile(dates=(ProfileDate("valid_until", date(2031, 5, 7), "07.05.31"),)),
                        chunks("Valid 07.05.31"), "f.pdf")
        assert [g.grade for g in p.graded] == [AS]

    @pytest.mark.parametrize("printed", ["07 MAY/MAI 2031", "07 MAI/MAY 2031", "7 Mayo/May 2031", "MAY/MAI 07 2031"])
    def test_a_bilingual_month(self, printed):
        assert lenient_date(printed) == date(2031, 5, 7)

    def test_a_bilingual_month_still_refuses_a_different_date(self):
        assert lenient_date("08 MAY/MAI 2031") != date(2031, 5, 7)


def test_the_tables_digest_covers_the_spaced_date_pattern(monkeypatch):
    import src.domain.drive_keys as keys
    import re
    before = index_tables_digest()
    monkeypatch.setattr(keys, "_DMY_SPACED", re.compile("x"))
    assert index_tables_digest() != before
