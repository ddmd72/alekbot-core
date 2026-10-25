"""drive_tier_explain — what a Search v2 card says about how it matched (USER_DRIVE_INDEXING_RFC §25.5.6, §25.13.6)."""
from src.domain.drive_enrichment import DriveProfile, HintItem, ProfileEntity
from src.domain.drive_index_params import WINDOW_MAX, WINDOW_PAD
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import about_keys_of, name_keys_v2, query_concepts, word_form_keys, word_keys
from src.domain.drive_tier_explain import explain, not_matched, passage_window
from src.domain.drive_tiers import Allowance, FileKeys, allowances, classify

C1 = ("f", 1, 0)


def _match(rec, name, query, chunk_text=None, hints=None, allowance=None):
    cs = query_concepts(query).concepts
    f = FileKeys("f", frozenset(about_keys_of(rec)), frozenset(name_keys_v2(name)),
                 {C1: frozenset(word_keys(chunk_text))} if chunk_text else {},
                 {C1: frozenset(k for i in hints for k in word_form_keys(i.text))} if hints else {})
    texts = {C1: chunk_text} if chunk_text else {}
    items = {C1: tuple(hints)} if hints else {}
    m = classify(f, cs, allowance or Allowance(), texts, items)
    return m, explain(m, rec, name, chunk_text, tuple(hints or ()))


ROME = DriveProfile("Colosseum entry", "Two entry tickets", "en", "ticket",
                    entities=(ProfileEntity("Rome", "place", "Rome"),), label_words=("ticket", "tickets"))


def test_about_card_names_the_place_and_the_name_word():
    rec = RecordKeyInputs(kind="pdf", profile=ROME)
    _, ex = _match(rec, "Colosseum tickets.pdf", "rome tickets")
    assert ex.sources == ('place "Rome" (named in the file)', 'name "tickets"')


def test_photo_location():
    rec = RecordKeyInputs(kind="image", place_forms=("Coimbra", "Коїмбра", "Portugal"))
    m, ex = _match(rec, "IMG_1.jpg", "про Коїмбру")
    assert m.tier.value == "about" and ex.sources == ('place "Коїмбра" (photo location)',)


def test_type_label_is_automatic():
    rec = RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "en", "receipt", label_words=("receipt",
                                                                                                      "receipts")))
    _, ex = _match(rec, "scan.pdf", "all my receipts")
    assert ex.sources == ('type label "receipts" (automatic)',)


def test_passage_words_join_into_one_source_and_the_window_spans_them():
    text = "Day 3. Pick up the tickets for the Vatican museums in Rome on Tuesday."
    m, ex = _match(RecordKeyInputs(kind="markdown"), "trip.md", "rome tickets", chunk_text=text)
    assert ex.sources == ('words "Rome", "tickets" in one passage',)
    assert len(ex.spans) == 2


def test_one_passage_word_is_singular():
    text = "Ми гуляли в Коїмбрі весь день."
    _, ex = _match(RecordKeyInputs(kind="markdown"), "note.md", "про Коїмбру", chunk_text=text)
    assert ex.sources == ('word "Коїмбрі" in one passage',)


def test_passage_keyword_with_its_evidence():
    text = "Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo."
    hint = HintItem("Rome", "keyword", "reserva en Roma, 3 noches", 15, 40)
    _, ex = _match(RecordKeyInputs(kind="pdf"), "reserva.pdf", "rome", chunk_text=text, hints=[hint])
    assert ex.sources == ('passage keyword "Rome", from "reserva en Roma, 3 noches"',)
    assert ex.spans == ((15, 40),)


def test_a_skeleton_says_by_spelling_in_another_script():
    text = "Notes from the Coimbra trip."
    m, ex = _match(RecordKeyInputs(kind="markdown"), "n.md", "про Коїмбру", chunk_text=text)
    assert m.tier.value == "translated"
    assert ex.sources == ('word "Coimbra" in one passage, by spelling in another script (automatic)',)


def test_not_matched_names_the_missed_words():
    cs = query_concepts("rome photo").concepts
    rec = RecordKeyInputs(kind="pdf", profile=ROME)
    m, _ = _match(rec, "x.pdf", "rome photo", allowance=allowances(cs, {"rome": 3, "photo": 80}, 100))
    assert not_matched(m) == ('not matched (common): "photo"',)


def test_passage_window_pads_and_caps():
    text = "a" * 1000 + " Rome " + "b" * 1000
    out = passage_window(text, [(1001, 1005)])
    assert len(out) == 2 * WINDOW_PAD + 4 and "Rome" in out
    wide = passage_window(text, [(10, 14), (1990, 1994)])
    assert len(wide) == WINDOW_MAX
    assert passage_window("line one\n\nline   two", [(0, 4)]) == "line one line two"
    assert passage_window(text, []) == ""
