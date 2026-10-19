"""Search v2 query normalisation, `drive_keys.query_concepts` (USER_DRIVE_INDEXING_RFC §25.5.1): phrase runs before
function words are dropped, number and month concepts, generic words removed and reported, strict-AND inputs (one
concept per remaining word, never dropped), at most MAX_CONCEPTS, every emitted list at most CONCEPT_KEYS_MAX."""
from src.domain.drive_index_params import CONCEPT_KEYS_MAX, MAX_CONCEPTS, QUERY_TERM_CHARS
from src.domain.drive_keys import FUNCTION_WORDS, GENERIC_WORDS, query_concepts


def _words(q):
    return [c.word for c in q.concepts]


def test_every_remaining_word_is_its_own_concept_with_keys():
    q = query_concepts("rome tickets vaccination")
    assert _words(q) == ["rome", "tickets", "vaccination"]
    for c in q.concepts:
        assert c.word_keys and c.about_keys and c.hint_keys and not c.is_number
        assert f"w:{c.word}" == c.word_keys[0]                  # the token as written comes first


def test_function_and_generic_words_are_removed_and_reported():
    q = query_concepts("all my receipts")
    assert _words(q) == ["receipts"] and q.ignored == ("all", "my") and q.cut == ()
    q = query_concepts("найди все документы про Барселону")
    assert _words(q) == ["барселону"]
    assert q.ignored == ("найди", "все", "документы", "про")


def test_generic_words_named_by_the_rfc():
    for word in ("documents", "files", "all", "everything", "всё", "все", "усі", "файли", "документы", "todo",
                 "todos"):
        assert word in GENERIC_WORDS, word


def test_function_words_cover_the_five_languages():
    for word in ("the", "про", "для", "від", "del", "pour"):
        assert word in FUNCTION_WORDS, word


def test_a_query_of_only_generic_words_has_no_concepts():
    q = query_concepts("all my files")
    assert q.concepts == () and q.ignored == ("all", "my", "files")


def test_numbers_come_first_then_words_in_query_order():
    q = query_concepts("trip 2023 invoice 4455")
    assert _words(q) == ["2023", "4455", "trip", "invoice"]
    assert [c.is_number for c in q.concepts] == [True, True, False, False]


def test_a_year_emits_number_value_and_year_keys():
    year = query_concepts("2023").concepts[0]
    # pn: a photo's visible text (M3); Δ60: `va:`/`ya:` beside `v:`/`y:` (a value or date the text does not hold)
    assert set(year.about_keys) == {"n:2023", "v:2023", "va:2023", "y:2023", "ya:2023", "pn:2023"}
    assert year.word_keys == ("n:2023",) and year.hint_keys == ()


def test_a_number_outside_the_year_range_emits_no_year_key():
    num = query_concepts("4455").concepts[0]
    assert set(num.about_keys) == {"n:4455", "v:4455", "va:4455", "pn:4455"}   # Δ60: `va:` beside `v:`


def test_an_id_is_one_concept_of_its_maximal_form():
    q = query_concepts("INV-2024/0012")
    assert _words(q) == ["inv-2024/0012"]
    assert q.concepts[0].word_keys == ("n:inv20240012",)


def test_a_month_with_a_year_is_one_month_concept_emitting_only_m():
    for text in ("March 2023", "березень 2023", "марта 2023", "marzo de 2023"):
        (c,) = query_concepts(text).concepts
        # Δ60: `ma:` beside `m:` (a date the text does not hold); still no `y:`/`v:` for a month
        assert c.is_number and c.about_keys == ("m:2023-03", "ma:2023-03") and c.word_keys == ("m:2023-03",), text
        assert c.hint_keys == ()


def test_a_full_date_is_a_date_concept_not_a_month():
    (c,) = query_concepts("15 March 2023").concepts
    assert c.word_keys == ("n:2023-03-15",) and "v:2023-03-15" in c.about_keys
    assert not any(k.startswith("m:") for k in c.about_keys)


def test_at_most_max_concepts_and_the_rest_is_cut():
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    q = query_concepts(" ".join(words))
    assert len(q.concepts) == MAX_CONCEPTS
    assert _words(q) == words[:MAX_CONCEPTS] and q.cut == tuple(words[MAX_CONCEPTS:])


def test_numbers_are_kept_before_words_under_the_cap():
    q = query_concepts("alpha bravo charlie delta echo foxtrot golf hotel india 12345")
    assert _words(q)[0] == "12345" and "india" in q.cut and "hotel" in q.cut


def test_every_list_is_unique_and_fetched_in_batches_of_at_most_thirty():
    """Review B1: the lists are uncut (coverage); the fetch cuts them into batches of at most 30."""
    from src.domain.drive_keys import fetch_batches
    q = query_concepts("медицинские записи по холестерину у доктора Иванова в Киеве")
    assert any(len(c.about_keys) > CONCEPT_KEYS_MAX for c in q.concepts)
    for c in q.concepts:
        for keys in (c.about_keys, c.word_keys, c.hint_keys):
            assert len(set(keys)) == len(keys)
            batches = fetch_batches(keys)
            assert all(len(b) <= CONCEPT_KEYS_MAX for b in batches)
            assert [k for b in batches for k in b] == list(keys)


def test_lists_hold_only_their_streams_families():
    for c in query_concepts("Barcelona trip").concepts:
        assert all(k[:2] in ("w:", "x:") for k in c.word_keys) and all(k.startswith("w:") for k in c.hint_keys)
        # xc F1 adds `ef:`; xc F3: a Latin query word never reads `xl:`/`exl:` skeletons (so none appear here);
        # §26.3 (Δ58): a query word emits both capture families, `cp:` and `cpa:`, as it emits `k:`; D40 `sl:` for a
        # scene name ("trip"); Δ60 `va:` beside `v:`
        assert all(k.split(":", 1)[0] + ":" in ("e:", "ef:", "ex:", "k:", "cp:", "cpa:", "sl:", "lw:", "tw:", "dw:",
                                                "lv:", "pw:", "pe:", "v:", "va:") for k in c.about_keys)
        assert not any(k.startswith(("xl:", "exl:")) for k in (*c.word_keys, *c.about_keys, *c.hint_keys))


def test_barcelona_trip_walk_through():
    q = query_concepts("Barcelona trip")
    assert _words(q) == ["barcelona", "trip"] and q.ignored == ()
    barcelona, trip = q.concepts
    assert "e:barcelona_trip" in barcelona.about_keys and "e:barcelona_trip" in trip.about_keys
    assert "x:barselon" in barcelona.word_keys and "ex:barselon" in barcelona.about_keys   # the c rule
    assert {"k:trip", "lw:trip", "tw:trip"} <= set(trip.about_keys)


def test_phrase_runs_are_built_before_function_words_are_removed():
    q = query_concepts("Le Havre port")
    assert _words(q) == ["havre", "port"] and q.ignored == ("le",)
    havre = q.concepts[0]
    assert "e:le_havr" in havre.about_keys
    assert "e:нью_йорк" in query_concepts("Нью-Йорке").concepts[0].about_keys


def test_a_run_ending_in_a_function_word_gives_no_phrase_key():
    havre = query_concepts("Havre de").concepts[0]
    assert not any(k.startswith("e:havr_") for k in havre.about_keys)


def test_a_repeated_word_is_one_concept():
    assert _words(query_concepts("rome Rome ROME")) == ["rome"]


def test_a_one_letter_word_is_ignored_and_a_two_letter_one_is_a_concept():
    """Review M2 reversed P1's first deviation: a 2-letter word is a concept with its exact form; 1 letter is not."""
    q = query_concepts("BMW X5 x")
    assert [c.word for c in q.concepts] == ["bmw", "x5"] and q.ignored == ("x",)
    assert q.concepts[1].word_keys == ("w:x5",)


def test_the_query_is_clipped_at_a_word_boundary():
    q = query_concepts("a" * 5 + " " + "word " * 400 + " lastword")
    assert "lastword" not in _words(q) + list(q.cut)
    assert len("a" * 5 + " " + "word " * 400) > QUERY_TERM_CHARS


def test_an_empty_query_has_no_concepts():
    q = query_concepts("   ")
    assert q.concepts == () and q.ignored == () and q.cut == ()


# -- the walk-throughs of §25.5.1, key side ------------------------------------------------------------------------

def test_walk_through_pro_barselonu_concepts():
    q = query_concepts("про Барселону")
    assert _words(q) == ["барселону"] and q.ignored == ("про",)
    c = q.concepts[0]
    assert {"w:барселону", "w:барселон", "x:barselon"} <= set(c.word_keys)
    assert {"e:барселону", "e:барселон"} <= set(c.about_keys)


def test_walk_through_medical_records_concepts():
    q = query_concepts("медицинские записи по холестерину")
    assert _words(q) == ["медицинские", "записи", "холестерину"] and q.ignored == ("по",)
    chol = q.concepts[2]
    assert "tw:холестерин" in chol.about_keys and "w:холестерин" in chol.word_keys
    assert "lw:запис" in q.concepts[1].about_keys and "lw:медицинские" in q.concepts[0].about_keys


def test_walk_through_paris_2023_concepts():
    q = query_concepts("Paris 2023")
    assert _words(q) == ["2023", "paris"]
    assert {"n:2023", "v:2023", "y:2023"} <= set(q.concepts[0].about_keys)
    assert "e:paris" in q.concepts[1].about_keys


def test_walk_through_receipts_and_expires_concepts():
    receipts = query_concepts("all my receipts").concepts[0]
    assert {"w:receipts", "w:receipt"} == set(receipts.word_keys[:2]) and "lw:receipt" in receipts.about_keys
    q = query_concepts("what expires")
    assert q.ignored == ("what",)
    assert "lv:expir" in q.concepts[0].about_keys
