"""Keys of what the text does not hold (USER_DRIVE_INDEXING_RFC §26.4, Δ60): a `not_in_text` value is keyed `va:`, a
date `ya:`/`ma:` (and its ISO form `va:`), an entity only `ef:`; `va:`/`ya:`/`ma:` are automatic-record keys (Labelled),
`ef:` stays By translation; a query emits them beside `v:`/`y:`/`m:`; a not-in-text value never puts a file in an exact
tier; `in_text_as` is the text's own value."""
from datetime import date

from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_key_sources import AboutField, RecordKeyInputs
from src.domain.drive_keys import about_keys_of, about_sources, query_concepts, source_class_of_about_key
from src.domain.drive_match import FAMILY_SOURCE_CLASS, KeyFamily, MatchTier, SourceClass
from src.domain.drive_tier_explain import explain
from src.domain.drive_tiers import Allowance, FileKeys, classify

IN, AS, NOT = Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value, Grade.NOT_IN_TEXT.value


def gv(field, value, grade):
    return GradedValue(field=field.value, value=value, grade=grade)


def keys_of(profile):
    return set(about_keys_of(RecordKeyInputs(kind="pdf", profile=profile)))


def _profile(**kw):
    return DriveProfile(title="t", what_it_is="w", language="en", doc_type="other", **kw)


def _tier(query, profile):
    keys = frozenset(about_keys_of(RecordKeyInputs(kind="pdf", profile=profile)))
    f = FileKeys(item_id="f", about_keys=keys, name_keys=frozenset(), chunk_keys={}, hint_keys={})
    match = classify(f, query_concepts(query).concepts, Allowance(), {}, {})
    return match.tier if match is not None else None


class TestFamilies:
    def test_the_new_families_are_automatic_record_keys(self):
        for family in (KeyFamily.VALUE_AUTO, KeyFamily.YEAR_AUTO, KeyFamily.MONTH_AUTO):
            assert FAMILY_SOURCE_CLASS[family] is SourceClass.AUTOMATIC_RECORD
        assert [f.value for f in (KeyFamily.VALUE_AUTO, KeyFamily.YEAR_AUTO, KeyFamily.MONTH_AUTO)] == [
            "va:", "ya:", "ma:"]

    def test_the_translation_class_of_ef_is_kept(self):
        assert FAMILY_SOURCE_CLASS[KeyFamily.ENTITY_FORM] is SourceClass.TRANSLATION
        assert source_class_of_about_key("va:abc") is SourceClass.AUTOMATIC_RECORD
        assert source_class_of_about_key("ya:2031") is SourceClass.AUTOMATIC_RECORD
        assert source_class_of_about_key("ma:2031-05") is SourceClass.AUTOMATIC_RECORD
        assert source_class_of_about_key("v:abc") is SourceClass.VERIFIED_RECORD


class TestRecordKeys:
    def test_a_date_in_the_text_is_keyed_v_y_m(self):
        keys = keys_of(_profile(dates=(ProfileDate("valid_until", date(2031, 5, 7)),),
                                graded=(gv(GradedField.DATE, "2031-05-07", IN),)))
        assert {"v:2031-05-07", "y:2031", "m:2031-05"} <= keys
        assert not {k for k in keys if k.startswith(("va:", "ya:", "ma:"))}

    def test_a_date_in_text_as_is_keyed_like_one_in_text(self):
        keys = keys_of(_profile(dates=(ProfileDate("valid_until", date(2031, 5, 7)),),
                                graded=(gv(GradedField.DATE, "2031-05-07", AS),)))
        assert {"v:2031-05-07", "y:2031", "m:2031-05"} <= keys

    def test_a_date_not_in_the_text_is_keyed_va_ya_ma(self):
        keys = keys_of(_profile(dates=(ProfileDate("valid_until", date(2031, 5, 7)),),
                                graded=(gv(GradedField.DATE, "2031-05-07", NOT),)))
        assert {"va:2031-05-07", "ya:2031", "ma:2031-05"} <= keys
        assert not {"v:2031-05-07", "y:2031", "m:2031-05"} & keys

    def test_a_value_not_in_the_text_is_keyed_va(self):
        kv = KeyValue("document_number", "X1234567A", "x1234567a")
        held = keys_of(_profile(key_values=(kv,), graded=(gv(GradedField.VALUE, "X1234567A", IN),)))
        missing = keys_of(_profile(key_values=(kv,), graded=(gv(GradedField.VALUE, "X1234567A", NOT),)))
        assert "v:x1234567a" in held and "va:x1234567a" not in held
        assert "va:x1234567a" in missing and "v:x1234567a" not in missing

    def test_an_entity_not_in_the_text_is_keyed_ef_only(self):
        e = ProfileEntity("Stockholm", "place", "Stockholm", forms=("Estocolmo",))
        keys = keys_of(_profile(entities=(e,), graded=(gv(GradedField.ENTITY, "Stockholm", NOT),)))
        assert "ef:stockholm" in keys and "ef:estocolmo" in keys
        assert not [k for k in keys if k.startswith("e:")]

    def test_an_entity_in_the_text_keeps_its_exact_keys(self):
        e = ProfileEntity("Stockholm", "place", "Stockholm", forms=("Estocolmo",))
        keys = keys_of(_profile(entities=(e,), graded=(gv(GradedField.ENTITY, "Stockholm", IN),)))
        assert "e:stockholm" in keys and "ef:estocolmo" in keys

    def test_an_entity_in_text_as_is_exact_only_in_the_spelling_the_text_has(self):
        e = ProfileEntity("Oslo", "place", "Oslo city", forms=("Oslo",), found=("Oslo",))
        keys = keys_of(_profile(entities=(e,), graded=(gv(GradedField.ENTITY, "Oslo city", AS),)))
        assert "e:oslo" in keys and "ef:oslo_citi" in keys

    def test_a_profile_stored_before_grading_keeps_the_old_keys(self):
        e = ProfileEntity("Oslo", "place", "Oslo")
        old = _profile(entities=(e,), dates=(ProfileDate("issued", date(2024, 3, 12)),),
                       key_values=(KeyValue("reference", "AB 12-34", "ab1234"),))
        keys = keys_of(old)
        assert {"e:oslo", "v:2024-03-12", "y:2024", "m:2024-03", "v:ab1234"} <= keys

    def test_sources_say_automatic_for_va_ya_ma(self):
        rec = RecordKeyInputs(kind="pdf", profile=_profile(
            dates=(ProfileDate("valid_until", date(2031, 5, 7)),), graded=(gv(GradedField.DATE, "2031-05-07", NOT),)))
        sources = about_sources(rec)
        for key in ("va:2031-05-07", "ya:2031", "ma:2031-05"):
            assert [(s.field, s.source_class) for s in sources[key]] == [(AboutField.DATE, SourceClass.AUTOMATIC_RECORD)]


class TestQuery:
    def test_a_year_emits_y_and_ya(self):
        about = set(query_concepts("2031").concepts[0].about_keys)
        assert {"y:2031", "ya:2031", "v:2031", "va:2031", "n:2031"} <= about

    def test_a_date_emits_v_and_va(self):
        about = set(query_concepts("07.05.2031").concepts[0].about_keys)
        assert {"v:2031-05-07", "va:2031-05-07"} <= about

    def test_a_month_emits_m_and_ma_but_the_chunk_stream_keeps_m_only(self):
        concept = query_concepts("May 2031").concepts[0]
        assert {"m:2031-05", "ma:2031-05"} <= set(concept.about_keys)
        assert concept.word_keys == ("m:2031-05",)

    def test_a_word_emits_va(self):
        about = set(query_concepts("passport").concepts[0].about_keys)
        assert {"v:passport", "va:passport"} <= about

    def test_an_id_emits_va_beside_v(self):
        about = set(query_concepts("X1234567A").concepts[0].about_keys)
        assert {"v:x1234567a", "va:x1234567a"} <= about


class TestTiers:
    def _profile_with(self, grade):
        return _profile(dates=(ProfileDate("valid_until", date(2031, 5, 7)),),
                        key_values=(KeyValue("document_number", "X1234567A", "x1234567a"),),
                        graded=(gv(GradedField.DATE, "2031-05-07", grade), gv(GradedField.VALUE, "X1234567A", grade)))

    def test_a_value_in_the_text_is_found_in_an_exact_tier(self):
        assert _tier("X1234567A", self._profile_with(IN)) in (MatchTier.ABOUT, MatchTier.MENTIONED)
        assert _tier("07.05.2031", self._profile_with(IN)) in (MatchTier.ABOUT, MatchTier.MENTIONED)

    def test_a_value_not_in_the_text_is_found_but_only_as_labelled(self):
        assert _tier("X1234567A", self._profile_with(NOT)) is MatchTier.LABELLED
        assert _tier("07.05.2031", self._profile_with(NOT)) is MatchTier.LABELLED
        assert _tier("2031", self._profile_with(NOT)) is MatchTier.LABELLED

    def test_an_entity_not_in_the_text_is_found_only_by_translation(self):
        e = ProfileEntity("Stockholm", "place", "Stockholm")
        p = _profile(entities=(e,), graded=(gv(GradedField.ENTITY, "Stockholm", NOT),))
        assert _tier("Stockholm", p) is MatchTier.TRANSLATED

    def test_the_match_source_does_not_call_a_model_value_the_files(self):
        profile = _profile(key_values=(KeyValue("document_number", "X1234567A", "x1234567a"),),
                           graded=(gv(GradedField.VALUE, "X1234567A", NOT),))
        rec = RecordKeyInputs(kind="pdf", profile=profile)
        keys = frozenset(about_keys_of(rec))
        f = FileKeys(item_id="f", about_keys=keys, name_keys=frozenset(), chunk_keys={}, hint_keys={})
        match = classify(f, query_concepts("X1234567A").concepts, Allowance(), {}, {})
        sources = explain(match, rec, "scan.pdf", None, ()).sources
        assert sources and "(automatic)" in sources[0]
