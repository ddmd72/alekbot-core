"""drive_profile.grade (USER_DRIVE_INDEXING_RFC §26.4, Δ59): every value is kept and graded against the stored text —
each field type x each grade, `seen_as`, the lenient date refusing a different date, prose numbers, native topics,
nothing dropped. Synthetic texts only."""
from datetime import date

import pytest

from src.domain import drive_profile as dp
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_grade import TextEvidence, lenient_date
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import PROFILE_DATES_MAX, PROFILE_ENTITIES_MAX

IN, AS, NOT = Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value, Grade.NOT_IN_TEXT.value

TEXT = ("Identity card of Maria Doe.\nCard number X 1234567 A.\nIssued 12.03.2024 by the Oslo office.\n"
        "Valid until 07 05 2031. Total 1 250,00 EUR. Seen on March 3, 2022. Contrato de alquiler de vivienda.")


def _chunks(text):
    return [DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=0, text=text, char_start=0, char_end=len(text),
                       kind="pdf")]


def _profile(**kw):
    base = dict(title="Card", what_it_is="A card", language="en", doc_type="id_document")
    base.update(kw)
    return DriveProfile(**base)


def _grade(profile, text=TEXT, name="card.pdf", **kw):
    return dp.grade(profile, _chunks(text), name, **kw)


def _by(profile, gfield):
    return {g.value: g for g in profile.graded if g.field == gfield.value}


class TestDates:
    def test_iso_among_the_text_terms_is_in_text(self):
        p, counts = _grade(_profile(dates=(ProfileDate("issued", date(2024, 3, 12), "12.03.2024"),)))
        assert _by(p, GradedField.DATE)["2024-03-12"].grade == IN
        assert counts.not_in_text == {}

    def test_printed_found_and_reading_to_the_same_date_is_in_text_as(self):
        # "March 3, 2022" is a month-name form; the strict term list lacks it when the printed form differs in spacing
        text = "Seen on 3 - 3 - 2022 in the office."
        p, _ = _grade(_profile(dates=(ProfileDate("event", date(2022, 3, 3), "3 - 3 - 2022"),)), text=text)
        g = _by(p, GradedField.DATE)["2022-03-03"]
        assert g.grade == AS and g.seen_as == "3 - 3 - 2022"

    def test_a_printed_form_that_reads_as_a_different_date_is_refused(self):
        text = "Seen on 3 - 4 - 2022 in the office."
        p, counts = _grade(_profile(dates=(ProfileDate("event", date(2022, 3, 4), "3 - 4 - 2022"),)), text=text)
        assert _by(p, GradedField.DATE)["2022-03-04"].grade == NOT          # day-first reads 2022-04-03
        assert counts.not_in_text == {GradedField.DATE.value: 1}

    def test_a_date_the_text_does_not_hold_is_kept_and_graded_not_in_text(self):
        p, counts = _grade(_profile(dates=(ProfileDate("valid_until", date(2040, 1, 1), "01.01.2040"),)))
        assert [d.iso for d in p.dates] == [date(2040, 1, 1)]               # nothing dropped
        assert _by(p, GradedField.DATE)["2040-01-01"].grade == NOT
        assert counts.not_in_text[GradedField.DATE.value] == 1

    def test_dates_keep_the_cap_and_the_relabelling(self):
        many = tuple(ProfileDate("bogus", date(2000 + i, 1, 1)) for i in range(PROFILE_DATES_MAX + 3))
        p, counts = _grade(_profile(dates=many))
        assert len(p.dates) == PROFILE_DATES_MAX and {d.label for d in p.dates} == {"other"}
        assert counts.labels == PROFILE_DATES_MAX


class TestLenientDate:
    @pytest.mark.parametrize("printed, iso", [
        ("07 05 2031", date(2031, 5, 7)), ("07-05-2031", date(2031, 5, 7)), ("7 / 5 / 2031", date(2031, 5, 7)),
        ("2031 05 07", date(2031, 5, 7)), ("05/31/2031", date(2031, 5, 31)),
        ("7 May 2031", date(2031, 5, 7)), ("May 7, 2031", date(2031, 5, 7)), ("7 de mayo de 2031", date(2031, 5, 7)),
        ("7 mai 2031", date(2031, 5, 7)), ("7 травня 2031", date(2031, 5, 7)), ("7 мая 2031 г.", date(2031, 5, 7)),
        ("7-May-2031", date(2031, 5, 7)),
    ])
    def test_reads_one_date(self, printed, iso):
        assert lenient_date(printed) == iso

    @pytest.mark.parametrize("printed", ["31 02 2031", "1 2", "07 05 31", "no date", "07 05 2031 08"])
    def test_refuses_what_is_not_a_date(self, printed):
        assert lenient_date(printed) is None

    def test_day_first_wins_over_month_first(self):
        assert lenient_date("03 04 2022") == date(2022, 4, 3)


class TestKeyValues:
    def test_number_term_is_in_text(self):
        p, _ = _grade(_profile(key_values=(KeyValue("amount_total", "1.250,00 EUR", "1250.00"),)))
        assert _by(p, GradedField.VALUE)["1.250,00 EUR"].grade == IN
        assert p.key_values[0].canonical == "1250.00"

    def test_letters_and_digits_with_separators_removed_is_in_text_as(self):
        p, _ = _grade(_profile(key_values=(KeyValue("document_number", "X1234567A", "x1234567a"),)))
        g = _by(p, GradedField.VALUE)["X1234567A"]
        assert g.grade == AS and g.seen_as == "X 1234567 A"
        assert p.key_values[0].canonical == "x1234567a"

    def test_a_value_inside_a_longer_run_is_not_found(self):
        p, _ = _grade(_profile(key_values=(KeyValue("other_id", "34567", "34567"),)), text="Code AB345678 only.")
        assert _by(p, GradedField.VALUE)["34567"].grade == NOT

    def test_a_missing_value_is_kept_and_counted(self):
        p, counts = _grade(_profile(key_values=(KeyValue("amount_total", "999.99 EUR", "999.99"),)))
        assert [k.printed for k in p.key_values] == ["999.99 EUR"]
        assert _by(p, GradedField.VALUE)["999.99 EUR"].grade == NOT and counts.not_in_text["value"] == 1

    def test_kinds_without_key_values_keep_none(self):
        p, _ = _grade(_profile(key_values=(KeyValue("amount_total", "1250.00", "1250.00"),)), key_values=False)
        assert p.key_values == ()

    def test_an_unknown_label_becomes_other_id(self):
        p, counts = _grade(_profile(key_values=(KeyValue("shoe_size", "1.250,00 EUR", "1250.00"),)))
        assert p.key_values[0].label == "other_id" and counts.labels == 1


class TestEntities:
    def test_printed_in_text_is_in_text_and_found_spellings_are_listed(self):
        e = ProfileEntity("Maria Doe", "person", "Maria Doe", forms=("Марія Доу",))
        p, _ = _grade(_profile(entities=(e,)))
        assert _by(p, GradedField.ENTITY)["Maria Doe"].grade == IN

    def test_printed_in_the_file_name_counts(self):
        e = ProfileEntity("Acme", "organization", "Acme")
        p, _ = _grade(_profile(entities=(e,)), name="Acme invoice.pdf")
        assert _by(p, GradedField.ENTITY)["Acme"].grade == IN

    def test_only_a_found_spelling_in_the_text_is_in_text_as(self):
        e = ProfileEntity("Oslo", "place", "Oslo city", forms=("Oslo",))
        p, _ = _grade(_profile(entities=(e,)))
        g = _by(p, GradedField.ENTITY)["Oslo city"]
        assert g.grade == AS and g.seen_as == "Oslo" and p.entities[0].found == ("Oslo",)

    def test_only_the_transliteration_skeleton_in_the_text_is_in_text_as(self):
        e = ProfileEntity("Coimbra", "place", "Coimbra")
        p, _ = _grade(_profile(entities=(e,)), text="Адреса: Коїмбра, вулиця Мільйонів.")
        g = _by(p, GradedField.ENTITY)["Coimbra"]
        assert g.grade == AS and g.seen_as == "Коїмбра"

    def test_absent_from_the_text_is_not_in_text_and_kept(self):
        e = ProfileEntity("Stockholm", "place", "Stockholm", forms=("Estocolmo",))
        p, counts = _grade(_profile(entities=(e,)))
        assert [x.name for x in p.entities] == ["Stockholm"] and p.entities[0].found == ()
        assert _by(p, GradedField.ENTITY)["Stockholm"].grade == NOT and counts.not_in_text["entity"] == 1

    def test_unknown_type_becomes_other_and_the_cap_applies(self):
        many = tuple(ProfileEntity(f"Name{i}", "wizard", f"Name{i}") for i in range(PROFILE_ENTITIES_MAX + 2))
        p, counts = _grade(_profile(entities=many))
        assert len(p.entities) == PROFILE_ENTITIES_MAX and {e.type for e in p.entities} == {"other"}
        assert counts.labels == PROFILE_ENTITIES_MAX


class TestProse:
    def test_numbers_of_title_and_what_it_is_are_graded(self):
        p, counts = _grade(_profile(what_it_is="Card number 9876543, valid until 2040-01-01"))
        numbers = _by(p, GradedField.NUMBER)
        assert numbers["9876543"].grade == NOT and counts.not_in_text["number"] >= 1

    def test_valid_until_prose_in_a_format_the_text_prints_differently_is_in_text_as(self):
        text = "Valid until 7 - 5 - 2031."                    # no strict date term: only the lenient reader sees it
        p, _ = _grade(_profile(what_it_is="Valid until 2031-05-07"), text=text)
        assert _by(p, GradedField.NUMBER)["2031-05-07"].grade == AS

    def test_a_number_already_graded_as_a_date_is_not_graded_twice(self):
        p, _ = _grade(_profile(what_it_is="Issued 2024-03-12", dates=(ProfileDate("issued", date(2024, 3, 12)),)))
        assert "2024-03-12" not in _by(p, GradedField.NUMBER)

    def test_descriptive_fields_and_word_lists_are_never_graded(self):
        p, _ = _grade(_profile(topics=("zebra",), topic_words=("zebra",), label_words=("zebra",),
                               domain_words=("zebra",), life_domains=("home",)))
        assert {g.field for g in p.graded} <= {f.value for f in GradedField}
        assert all(g.value != "zebra" for g in p.graded)


class TestNativeTopics:
    def test_phrase_as_written_is_in_text(self):
        p, _ = _grade(_profile(topics_native=("contrato de alquiler",)))
        assert _by(p, GradedField.TOPIC_NATIVE)["contrato de alquiler"].grade == IN

    def test_every_word_among_the_text_word_keys_is_in_text_as(self):
        p, _ = _grade(_profile(topics_native=("vivienda alquiler",)))
        assert _by(p, GradedField.TOPIC_NATIVE)["vivienda alquiler"].grade == AS

    def test_a_topic_whose_words_the_text_lacks_is_not_in_text(self):
        p, counts = _grade(_profile(topics_native=("submarino",)))
        assert _by(p, GradedField.TOPIC_NATIVE)["submarino"].grade == NOT and counts.not_in_text["topic_native"] == 1

    def test_english_topics_are_not_graded_twice(self):
        p, _ = _grade(_profile(topics=("rental contract",), topics_native=("contrato de alquiler",)))
        assert [g.value for g in p.graded if g.field == GradedField.TOPIC_NATIVE.value] == ["contrato de alquiler"]


class TestWholeProfile:
    def test_nothing_is_dropped(self):
        profile = _profile(
            dates=(ProfileDate("issued", date(2024, 3, 12), "12.03.2024"), ProfileDate("event", date(1999, 9, 9))),
            entities=(ProfileEntity("Oslo", "place", "Oslo"), ProfileEntity("Nowhere", "place", "Nowhere")),
            key_values=(KeyValue("amount_total", "1.250,00 EUR", "1250.00"), KeyValue("other_id", "ZZ99", "zz99")))
        p, counts = _grade(profile)
        assert len(p.dates) == 2 and len(p.entities) == 2 and len(p.key_values) == 2
        assert counts.not_in_text == {"date": 1, "entity": 1, "value": 1}

    def test_regrading_a_graded_profile_is_stable(self):
        profile = _profile(entities=(ProfileEntity("Oslo", "place", "Oslo"),),
                           key_values=(KeyValue("document_number", "X1234567A", "x1234567a"),),
                           dates=(ProfileDate("valid_until", date(2031, 5, 7), "07 05 2031"),))
        once, _ = _grade(profile)
        twice, _ = _grade(once)
        assert twice == once

    def test_validity_words_only_when_the_validity_date_is_held(self):
        held = _profile(dates=(ProfileDate("valid_until", date(2031, 5, 7), "07 05 2031"),), validity_words=("expires",))
        missing = _profile(dates=(ProfileDate("valid_until", date(2040, 1, 1), "01.01.2040"),),
                           validity_words=("expires",))
        assert _grade(held)[0].validity_words == ("expires",)
        assert _grade(missing)[0].validity_words == ()


class TestNotInTextDatesNeverMoveAFile:
    def test_valid_until_and_issued_skip_a_not_in_text_date(self):
        p, _ = _grade(_profile(dates=(ProfileDate("valid_until", date(2040, 1, 1), "01.01.2040"),
                                      ProfileDate("issued", date(2001, 1, 1), "01.01.2001"))))
        assert dp.valid_until_of(p) is None and dp.issued_of(p) is None

    def test_a_date_in_text_as_counts(self):
        text = "Seen on 3 - 3 - 2022."
        p, _ = _grade(_profile(dates=(ProfileDate("valid_until", date(2022, 3, 3), "3 - 3 - 2022"),)), text=text)
        assert dp.valid_until_of(p) == date(2022, 3, 3)

    def test_due_is_the_fallback_only_when_it_is_held(self):
        p, _ = _grade(_profile(dates=(ProfileDate("valid_until", date(2040, 1, 1), "01.01.2040"),
                                      ProfileDate("due", date(2024, 3, 12), "12.03.2024"))))
        assert dp.valid_until_of(p) == date(2024, 3, 12)

    def test_a_profile_stored_before_grading_counts_every_date(self):
        old = _profile(dates=(ProfileDate("valid_until", date(2040, 1, 1)),))
        assert old.graded == () and dp.valid_until_of(old) == date(2040, 1, 1)


class TestBrief:
    def test_date_amount_and_parties_are_graded(self):
        from src.domain.drive_index import DocumentBrief
        from datetime import datetime, timezone
        brief = DocumentBrief(description="d", doc_date=datetime(2024, 3, 12, tzinfo=timezone.utc),
                              parties=("Maria Doe", "Nobody Corp"), amount="1 250,00 EUR")
        graded = {(g.field, g.value): g.grade for g in dp.grade_brief(brief, _chunks(TEXT), "scan.pdf")}
        assert graded[("date", "2024-03-12")] == IN
        assert graded[("value", "1 250,00 EUR")] == IN
        assert graded[("entity", "Maria Doe")] == IN and graded[("entity", "Nobody Corp")] == NOT

    def test_a_date_the_text_prints_another_way_is_in_text_as(self):
        from src.domain.drive_index import DocumentBrief
        from datetime import datetime, timezone
        brief = DocumentBrief(doc_date=datetime(2022, 3, 3, tzinfo=timezone.utc))
        graded = dp.grade_brief(brief, _chunks("Seen on 3 - 3 - 2022."), "scan.pdf")
        assert [(g.field, g.grade) for g in graded] == [("date", AS)]


class TestEvidence:
    def test_prints_date_reads_month_names(self):
        ev = TextEvidence(["Seen on March 3, 2022."], "")
        assert ev.prints_date(date(2022, 3, 3)) and not ev.prints_date(date(2022, 3, 4))
