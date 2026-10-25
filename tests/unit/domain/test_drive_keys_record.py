"""Search v2 record keys: `about_keys_of`, `about_sources` and the `k:` table (USER_DRIVE_INDEXING_RFC §25.2.4,
§25.13.5), plus the key side of the §25.5.1 walk-throughs. Verified and automatic sources never share a prefix (the
arbiter's M3): a tier is decided from the lean record's keys alone."""
from datetime import date, datetime, timezone

import pytest

from src.domain.drive_enrichment import DriveProfile, KeyValue, PhotoObject, ProfileDate, ProfileEntity
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import ABOUT_KEYS_PER_RECORD
from src.domain.drive_key_sources import AboutField, RecordKeyInputs
from src.domain.drive_keys import (
    about_keys_capped, about_keys_of, about_sources, query_concepts, source_class_of_about_key, word_form_keys,
    word_keys,
)
from src.domain.drive_labels import KIND_WORDS
from src.domain.drive_match import SourceClass

UTC = timezone.utc


def _profile(**kw) -> DriveProfile:
    base = dict(title="", what_it_is="", language="en", doc_type="other")
    base.update(kw)
    return DriveProfile(**base)


def _covers(query: str, rec: RecordKeyInputs) -> bool:
    keys = set(about_keys_of(rec))
    return all(set(c.about_keys) & keys for c in query_concepts(query).concepts)


# -- the k: table --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(KIND_WORDS))
def test_every_kind_word_is_keyed_and_found_by_its_query(kind):
    rec = RecordKeyInputs(kind=kind)
    searchable = [w for w in KIND_WORDS[kind] if query_concepts(w).concepts]
    for word in searchable:
        assert _covers(word, rec), (kind, word)
    # "document" words are generic search words: a format goes in drive_kind (D25); their k: keys stay index-side
    assert searchable or kind in {"docx", "doc", "odt", "rtf"}, kind


def test_photo_words_only_for_an_image_triaged_as_a_photo():
    photo = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO)
    scan = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.TEXT)
    for word in ("photo", "фото", "foto", "світлина", "photos"):
        assert _covers(word, photo) and not _covers(word, scan), word
    assert _covers("image", scan) and _covers("picture", photo)


def test_kind_words_pass_through_the_forms():
    assert _covers("presentations", RecordKeyInputs(kind="pptx"))
    assert _covers("таблиці", RecordKeyInputs(kind="xlsx"))
    assert _covers("web page", RecordKeyInputs(kind="html"))
    assert not _covers("folder", RecordKeyInputs(kind="pdf"))


def test_a_kind_outside_the_table_has_no_kind_keys():
    assert not any(k.startswith("k:") for k in about_keys_of(RecordKeyInputs(kind="code")))


# -- families -------------------------------------------------------------------------------------------------------

def test_labels_are_stored_for_grouping():
    rec = RecordKeyInputs(kind="pdf", valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                          profile=_profile(doc_type="insurance_policy", life_domains=("vehicle", "finance")))
    keys = set(about_keys_of(rec))
    assert {"l:type:insurance_policy", "l:domain:vehicle", "l:domain:finance", "l:valid"} <= keys


def test_values_and_dates():
    rec = RecordKeyInputs(kind="pdf", effective_date=datetime(2024, 3, 15, tzinfo=UTC), profile=_profile(
        key_values=(KeyValue("invoice_number", "INV-2024/0012", "INV20240012"),),
        dates=(ProfileDate("issued", date(2024, 3, 15)), ProfileDate("due", date(2024, 4, 30)))))
    keys = set(about_keys_of(rec))
    assert {"v:inv20240012", "v:2024-03-15", "v:2024-04-30", "y:2024", "m:2024-03", "m:2024-04"} <= keys
    assert _covers("INV-2024/0012", rec) and _covers("2024", rec) and _covers("April 2024", rec)
    assert not _covers("May 2024", rec)


def test_effective_date_gives_year_and_month():
    keys = set(about_keys_of(RecordKeyInputs(kind="image", effective_date=datetime(2023, 7, 2, tzinfo=UTC))))
    assert {"y:2023", "m:2023-07"} <= keys and not any(k.startswith("v:") for k in keys)


def test_validity_words_only_when_valid_until_is_set():
    profile = _profile(validity_words=("expires", "valid until", "caduca"))
    assert _covers("expires", RecordKeyInputs(kind="pdf", profile=profile,
                                              valid_until=datetime(2027, 1, 1, tzinfo=UTC)))
    assert not _covers("expires", RecordKeyInputs(kind="pdf", profile=profile))


def test_entities_single_token_full_form_set_and_phrases_per_script():
    rec = RecordKeyInputs(kind="pdf", profile=_profile(entities=(
        ProfileEntity("The Hague", "place", "The Hague"),
        ProfileEntity("Нью-Йорк", "place", "Нью-Йорке"),
        ProfileEntity("Coimbra", "place", "Coimbra", ("Коїмбра",)),
    )))
    keys = set(about_keys_of(rec))
    # xc F1: "Коїмбра" is a form the file does not print (`found` empty), so it is the index's own `ef:`, not `e:`
    assert {"e:the_hagu", "e:нью_йорк", "e:coimbra", "ef:коїмбра", "ef:коїмбр"} <= keys
    assert not {"e:коїмбра", "e:коїмбр"} & keys
    # xc F3: the Latin entities' skeletons are `exl:`, the Cyrillic form's is `ex:`; the compound joined
    assert {"exl:the_hah", "exl:koimbr", "ex:koimbr", "e:ньюйорк"} <= keys
    # a spelling the verifier found in the text stays exact
    found = set(about_keys_of(RecordKeyInputs(kind="pdf", profile=_profile(entities=(
        ProfileEntity("Coimbra", "place", "Coimbra", ("Коїмбра",), ("Коїмбра",)),)))))
    assert {"e:коїмбра", "e:коїмбр"} <= found and not {"ef:коїмбра", "ef:коїмбр"} & found
    assert _covers("The Hague", rec) and _covers("Нью-Йорке", rec) and _covers("Коїмбрі", rec)


def test_entity_and_list_caps_keep_the_earliest_entries():
    entities = tuple(ProfileEntity(f"Name{chr(97 + i)}x", "person", f"Name{chr(97 + i)}x") for i in range(20))
    keys = set(about_keys_of(RecordKeyInputs(kind="pdf", profile=_profile(entities=entities))))
    assert "e:nameax" in keys and "e:namelx" in keys and "e:namemx" not in keys    # 12 entities


def test_the_record_cap_counts_capped_keys():
    words = tuple(f"word{chr(97 + i)}{chr(97 + j)}" for i in range(26) for j in range(26))
    rec = RecordKeyInputs(kind="pdf", profile=_profile(topic_words=words[:24], label_words=words[24:48],
                                                       domain_words=words[48:72]),
                          photo_description=" ".join(words))
    keys, capped = about_keys_capped(rec)
    assert len(keys) == ABOUT_KEYS_PER_RECORD and capped
    assert about_keys_of(rec) == keys
    assert not about_keys_capped(RecordKeyInputs(kind="pdf"))[1]


# -- verified and automatic keys are told apart by prefix (M3) ----------------------------------------------------

def test_photo_objects_and_visible_text_numbers_have_their_own_automatic_prefixes():
    rec = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO,
                          photo_objects=(PhotoObject("Brandenburger Tor", ("Бранденбурзькі ворота",)),),
                          visible_text="Gate 12345")
    keys = set(about_keys_of(rec))
    assert {"pe:brandenburger_tor", "pe:бранденбурзьк_ворот"} <= keys
    assert not any(k.startswith(("e:", "ex:")) for k in keys)
    assert "pn:12345" in keys and "pw:gate" in keys and not any(k.startswith("n:") for k in keys)
    assert _covers("Brandenburger Tor", rec) and _covers("12345", rec)


@pytest.mark.parametrize("prefix,cls", [
    ("e:", SourceClass.VERIFIED_RECORD), ("v:", SourceClass.VERIFIED_RECORD), ("y:", SourceClass.VERIFIED_RECORD),
    ("m:", SourceClass.VERIFIED_RECORD), ("k:", SourceClass.VERIFIED_RECORD),
    ("lw:", SourceClass.AUTOMATIC_RECORD), ("tw:", SourceClass.AUTOMATIC_RECORD),
    ("dw:", SourceClass.AUTOMATIC_RECORD), ("lv:", SourceClass.AUTOMATIC_RECORD),
    ("pw:", SourceClass.AUTOMATIC_RECORD), ("pe:", SourceClass.AUTOMATIC_RECORD),
    ("pn:", SourceClass.AUTOMATIC_RECORD), ("ex:", SourceClass.TRANSLATION),
])
def test_every_queried_about_prefix_has_one_source_class(prefix, cls):
    assert source_class_of_about_key(prefix + "x") is cls


def test_label_keys_have_no_source_class():
    assert source_class_of_about_key("l:valid") is None


def test_every_about_key_has_a_source_of_its_prefixs_class():
    rec = RecordKeyInputs(
        kind="image", triage_verdict=TriageVerdict.PHOTO, effective_date=datetime(2023, 5, 1, tzinfo=UTC),
        place_forms=("Berlin", "Берлін", "Germany"), photo_description="A cafe at night",
        photo_objects=(PhotoObject("TV Tower"),), photo_scene="street", visible_text="Straße 10115",
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
        profile=_profile(doc_type="ticket", life_domains=("travel",), topic_words=("trip",),
                         label_words=("ticket",), domain_words=("travel",), validity_words=("expires",),
                         entities=(ProfileEntity("Pergamonmuseum", "place", "Pergamonmuseum"),),
                         key_values=(KeyValue("ticket_number", "T-555", "t555"),),
                         dates=(ProfileDate("event", date(2023, 5, 1)),)))
    sources = about_sources(rec)
    keys = about_keys_of(rec)
    assert set(sources) == set(keys)
    for key in keys:
        cls = source_class_of_about_key(key)
        assert all(s.source_class is cls for s in sources[key]), key


# -- the walk-throughs of §25.5.1, record side ---------------------------------------------------------------------

def test_walk_through_pro_barselonu_record_side():
    concept = query_concepts("про Коїмбру").concepts[0]
    photo = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO,
                            place_forms=("Coimbra", "Коїмбра", "Portugal", "Португалія"))
    all_hits = set(concept.about_keys) & set(about_keys_of(photo))
    hits = {k for k in all_hits if k.startswith("e:")}
    assert {"e:коїмбра", "e:коїмбр"} & hits
    sources = about_sources(photo)
    # Every hit, native or skeleton, comes from the place (test reviewer: the field check covers all hits again).
    assert {s.field for k in all_hits for s in sources[k]} == {AboutField.PLACE}
    assert all(s.source_class is SourceClass.VERIFIED_RECORD for k in hits for s in sources[k])
    skeletons = all_hits - hits                                      # review M8: a skeleton hit is a translation
    assert skeletons and all(k.startswith(("ex:", "exl:")) for k in skeletons)       # xc F3: Latin side is `exl:`
    assert all(s.source_class is SourceClass.TRANSLATION for k in skeletons for s in sources[k])

    doc = RecordKeyInputs(kind="pdf", profile=_profile(entities=(
        ProfileEntity("Coimbra", "place", "Коїмбра", ("Коїмбра",)),)))
    hits = set(concept.about_keys) & set(about_keys_of(doc))
    assert "e:коїмбр" in hits                                       # the printed form: exact
    doc_sources = about_sources(doc)
    assert {s.field for k in hits if k.startswith("e:") for s in doc_sources[k]} == {AboutField.ENTITY}
    # xc F1: the unprinted name "Coimbra" adds only its skeletons, as the index's own entity form
    assert {s.field for k in hits for s in doc_sources[k]} == {AboutField.ENTITY, AboutField.ENTITY_FORM}

    assert set(concept.word_keys) & set(word_keys("Тиждень у Коїмбрі, спека."))            # mentioned in
    assert set(concept.hint_keys) & set(word_form_keys("Коїмбра"))                        # by translation


def test_walk_through_medical_records_record_side():
    rec = RecordKeyInputs(kind="pdf", profile=_profile(
        doc_type="medical_record", language="es",
        topic_words=("cholesterol", "colesterol", "холестерин", "холестерол"),
        label_words=("medical record", "medical records", "historia clínica", "медична картка",
                     "медицинская запись", "медицинские записи")))
    keys = set(about_keys_of(rec))
    concepts = query_concepts("медицинские записи по холестерину").concepts
    assert all(set(c.about_keys) & keys for c in concepts)
    hits = {k for c in concepts for k in set(c.about_keys) & keys}
    assert "tw:холестерин" in hits
    assert {source_class_of_about_key(k) for k in hits} == {SourceClass.AUTOMATIC_RECORD}
    assert {s.field for k in hits for s in about_sources(rec)[k]} == {AboutField.TOPIC_WORD, AboutField.LABEL_WORD}


def test_walk_through_paris_2023_record_side():
    photo = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO,
                            effective_date=datetime(2023, 6, 3, tzinfo=UTC), place_forms=("Berlin", "Берлін", "Germany"))
    assert _covers("Berlin 2023", photo) and _covers("Берлін 2023", photo)
    assert not _covers("Berlin 2022", photo)


def test_walk_through_receipts_record_side():
    receipt = RecordKeyInputs(kind="pdf", profile=_profile(
        doc_type="receipt", label_words=("receipt", "receipts", "recibo", "квитанция", "квитанции", "квитанція")))
    assert _covers("all my receipts", receipt) and _covers("квитанции", receipt) and _covers("recibos", receipt)
    concept = query_concepts("receipts").concepts[0]
    assert set(concept.word_keys) & set(word_keys("Keep the receipt for the warranty."))


def test_walk_through_expires_record_side():
    rec = RecordKeyInputs(kind="pdf", valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                          profile=_profile(validity_words=("valid until", "expires", "caduca", "діє до", "истекает")))
    hits = set(query_concepts("what expires").concepts[0].about_keys) & set(about_keys_of(rec))
    assert "lv:expir" in hits
    assert {s.field for k in hits for s in about_sources(rec)[k]} == {AboutField.VALIDITY_WORD}
