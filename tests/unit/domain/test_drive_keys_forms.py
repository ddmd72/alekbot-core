"""Search v2 keyer: per-token form sets (USER_DRIVE_INDEXING_RFC §25.2.4) and the pinned normaliser groups of
§25.13.4. Every word of a pinned group must share a form with every other word of its group, on the index side
(`word_keys` of a text holding it) and on the query side (`query_concepts`); the negative pins must share none."""
import pytest

from src.domain.drive_keys import all_forms, query_concepts, token_forms, token_spans, word_keys

CYRILLIC_GROUPS = [
    ("Барселона", "Барселону", "Барселоне", "Барселоной"),
    ("Париж", "Париже"),
    ("Рим", "Риме"),
    ("Киев", "Киеве"),
    ("Київ", "Києві"),
    ("Львів", "Львові"),
    ("Харків", "Харкові"),
    ("Иванов", "Иванова", "Ивановым"),
    ("Петров", "Петрова"),
    ("Сергей", "Сергея"),
    ("Андрей", "Андрея"),
    ("музей", "музея"),
    ("альбом", "альбома"),
    ("диплом", "диплома"),
    ("герой", "героя"),
    ("Ростов", "Ростове"),
    ("Чехов", "Чехова"),
    ("отель", "отеля"),
    ("кровь", "крови"),
    ("Казань", "Казани"),
    ("квиток", "квитки"),
    ("чек", "чеки"),
    ("холестерин", "холестерину"),
]
LATIN_GROUPS = [
    ("house", "houses"),
    ("lease", "leases"),
    ("bus", "buses"),
    ("glass", "glasses"),
    ("cache", "caches"),
    ("movie", "movies"),
    ("policy", "policies"),
    ("box", "boxes"),
    ("receipt", "receipts"),
    ("invoice", "invoices"),
]
NEGATIVE_PINS = [("барсетка", "Барселона"), ("Валентина", "Валенсия")]


def _forms(word: str) -> set:
    (token, _, _), = token_spans(word)
    return set(all_forms(token))


def _meets(query: str, text: str) -> set:
    """The keys a query word shares with a text (any family): native or skeleton."""
    (concept,) = query_concepts(query).concepts
    return set(concept.word_keys) & set(word_keys(text))


def _pairs(group):
    return [(a, b) for a in group for b in group if a != b]


@pytest.mark.parametrize("a,b", [p for g in CYRILLIC_GROUPS + LATIN_GROUPS for p in _pairs(g)])
def test_a_pinned_group_shares_a_form(a, b):
    assert _forms(a) & _forms(b), (a, sorted(_forms(a)), b, sorted(_forms(b)))


@pytest.mark.parametrize("a,b", [p for g in CYRILLIC_GROUPS + LATIN_GROUPS for p in _pairs(g)])
def test_a_pinned_group_meets_from_query_to_index(a, b):
    """The query typed in one form finds a text holding another: the concept's word keys meet the chunk's."""
    (concept,) = query_concepts(a).concepts
    assert set(concept.word_keys) & set(word_keys(f"Текст про {b} и ещё что-то."))


@pytest.mark.parametrize("a,b", NEGATIVE_PINS)
def test_a_negative_pin_shares_no_form(a, b):
    assert not _forms(a) & _forms(b), sorted(_forms(a) & _forms(b))


@pytest.mark.parametrize("a,b", NEGATIVE_PINS)
def test_a_negative_pin_does_not_meet_from_query_to_index(a, b):
    (concept,) = query_concepts(a).concepts
    assert not set(concept.word_keys) & set(word_keys(f"Текст про {b}."))


def test_nyu_york_is_one_compound_word_meeting_its_locative():
    """A hyphen compound is one query word (review m5); its parts' phrase is among its record keys."""
    (concept,) = query_concepts("Нью-Йорк").concepts
    assert concept.word == "ньюйорк"
    assert set(concept.word_keys) & set(word_keys("Мы были в Нью-Йорке"))
    assert "e:нью_йорк" in concept.about_keys


@pytest.mark.parametrize("word,expected", [
    ("house", {"house", "hous"}), ("houses", {"houses", "hous"}),
    ("lease", {"lease", "leas"}), ("leases", {"leases", "leas"}),
    ("movie", {"movie", "movi"}), ("movies", {"movies", "movi"}),
    ("policy", {"policy", "polici"}), ("policies", {"policies", "polici"}),
    ("receipt", {"receipt"}), ("receipts", {"receipts", "receipt"}),
    ("paris", {"paris"}), ("analysis", {"analysis"}), ("taxis", {"taxis"}), ("menus", {"menus"}),
    ("expires", {"expires", "expir"}), ("glasses", {"glasses", "glass"}), ("boxes", {"boxes", "box"}),
])
def test_latin_own_forms_follow_the_corrected_plural_rule(word, expected):
    assert set(token_forms(word)) == expected


def test_latin_forms_are_accent_stripped():
    assert set(token_forms("résumé")) == {"resume", "resum"}
    (token, _, _), = token_spans("Œuvre")
    assert "oeuvre" in token_forms(token)


@pytest.mark.parametrize("word,form", [
    ("барселону", "барселон"),       # (b) an ending stripped
    ("барселоной", "барселон"),      # (b) the longest ending
    ("сергей", "серге"),             # (c) final й
    ("кровь", "кров"),               # (c) final ь
    ("квиток", "квитк"),             # (d) fleeting -ок
    ("отец", "отц"),                 # (d) fleeting -ец
    ("ремень", "ремн"),              # (d) fleeting -ень, then the soft sign
    ("київ", "києв"),                # (e) ї -> є
    ("харків", "харков"),            # (e) і -> о
    ("харків", "харк"),              # (e) then (b)
    ("львів", "льв"),
])
def test_cyrillic_forms(word, form):
    assert form in token_forms(word)


def test_cyrillic_forms_are_never_truncated():
    """No prefix cut: a form is the token, the token less one listed ending or soft letter, a fleeting-vowel or an
    alternation form — never a fixed-length stem."""
    assert set(token_forms("барсетка")) == {"барсетка", "барсетк"}
    assert set(token_forms("барселона")) == {"барселона", "барселон"}


def test_forms_under_three_letters_are_not_kept():
    assert token_forms("по") == ()
    assert "ки" not in token_forms("киев")
    assert all(len(f) >= 3 for f in token_forms("моей"))


def test_the_alternation_needs_a_stem_of_four_letters():
    assert token_forms("кіт") == ("кіт",)


def test_the_index_stores_no_transliterated_spelling():
    """Review B2: native forms plus one skeleton per word; the variants are generated on the query side."""
    assert all_forms("барселону") == token_forms("барселону")
    keys = word_keys("Барселону")
    assert [k for k in keys if k.startswith("x:")] == ["x:barselon"]
    assert not any(k.startswith("w:") and k[2:].isascii() for k in keys)


@pytest.mark.parametrize("latin,cyrillic", [
    ("barcelona", "барселона"),        # c before e -> с
    ("valencia", "валенсиа"),          # c before i -> с
    ("cycle", "сыкле"),                # c before y -> с; the second c before l -> к
    ("costa", "коста"),                # c elsewhere -> к
    ("chekhov", "чехов"),              # ch is still ч
])
def test_latin_to_cyrillic_applies_the_c_rule(latin, cyrillic):
    from src.domain.drive_key_forms import lat_to_cyr_ru
    assert lat_to_cyr_ru(latin) == cyrillic


def test_latin_spelling_meets_cyrillic_text_only_through_a_skeleton():
    """Across scripts the match is a skeleton (`x:`), never a native form: a translation, not exact (M8)."""
    assert _meets("Barcelona", "Были в Барселоне в мае.") == {"x:barselon"}


def test_cyrillic_query_meets_latin_text_only_through_a_skeleton():
    # xc F3: the text is Latin, so its skeleton is `xl:`, which a Cyrillic query word reads
    assert _meets("Барселону", "A week in Barcelona.") == {"xl:barselon"}


def test_digits_split_from_letters():
    assert [t for t, _, _ in token_spans("ред3")] == ["ред", "3"]
    keys = set(word_keys("ред3"))
    assert {"w:ред", "w:3"} <= keys


def test_a_short_number_is_a_word_form_and_a_long_one_a_number_term():
    assert token_forms("3") == ("3",) and token_forms("12") == ("12",)
    assert token_forms("123") == ()
    keys = set(word_keys("page 7 of invoice 12345"))
    assert "w:7" in keys and "n:12345" in keys and "w:12345" not in keys


def test_tokens_split_like_name_keys_except_case_and_apostrophe():
    """Separators and digit boundaries as `drive_name_keys`; a case boundary no longer splits (M1) and a Ukrainian
    apostrophe stays inside the word (M4); ё folds to е (m7)."""
    # (the v1 `_tokens` is gone, xc m4: its outputs on these texts are the literals)
    for text, tokens in (("Нью-Йорк", ["нью", "йорк"]), ("IMG_2210.jpg", ["img", "2210", "jpg"]),
                         ("Crème brûlée 2024", ["crème", "brûlée", "2024"]), ("Київ — «Львів»", ["київ", "львів"]),
                         ("Peter's house", ["peter", "s", "house"])):
        assert [t for t, _, _ in token_spans(text)] == tokens, text
    assert [t for t, _, _ in token_spans("myFileName v2")] == ["myfilename", "v", "2"]
    assert [t for t, _, _ in token_spans("Пётр сім'я")] == ["петр", "сімʼя"]
    assert [t for t, _, _ in token_spans("Пётр's ʼдім")] == ["петр", "s", "дім"]   # a Latin ' still separates


def test_token_spans_point_into_the_original_text():
    text = "Hotel in Barcelona, 3 nights"
    for token, start, end in token_spans(text):
        assert text[start:end].casefold() == token


def test_symbols_separate_tokens():
    assert [t for t, _, _ in token_spans("100€ total")] == ["100", "total"]
