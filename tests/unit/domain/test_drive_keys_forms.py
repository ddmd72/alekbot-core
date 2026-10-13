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


def test_nyu_york_meets_its_locative_as_a_phrase_and_per_token():
    concepts = query_concepts("Нью-Йорк").concepts
    assert [c.word for c in concepts] == ["нью", "йорк"]
    keys = set(word_keys("Мы были в Нью-Йорке"))
    assert all(set(c.word_keys) & keys for c in concepts)
    assert all("e:нью_йорк" in c.about_keys for c in concepts)


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


def test_transliterations_of_cyrillic_forms_a_and_b():
    forms = all_forms("барселону")
    assert {"barselonu", "barselon"} <= set(forms)
    own = token_forms("барселону")
    assert forms[:len(own)] == own                      # own forms first, then the transliterations


def test_ukrainian_national_transliteration_is_among_the_forms():
    assert "kyiv" in all_forms("київ")
    assert "kharkiv" in all_forms("харків")


@pytest.mark.parametrize("latin,cyrillic", [
    ("barcelona", "барселона"),        # c before e -> с
    ("valencia", "валенсиа"),          # c before i -> с
    ("cycle", "сыкл"),                 # c before y -> с; the second c before l -> к
    ("costa", "коста"),                # c elsewhere -> к
    ("chekhov", "чехов"),              # ch is still ч
])
def test_latin_to_cyrillic_applies_the_c_rule(latin, cyrillic):
    assert cyrillic in all_forms(latin)


def test_latin_spelling_meets_cyrillic_text_through_the_c_rule():
    (concept,) = query_concepts("Barcelona").concepts
    assert set(concept.word_keys) & set(word_keys("Были в Барселоне в мае."))


def test_cyrillic_query_meets_latin_text_through_the_index_transliteration():
    (concept,) = query_concepts("Барселону").concepts
    assert set(concept.word_keys) & set(word_keys("A week in Barcelona."))


def test_digits_split_from_letters():
    assert [t for t, _, _ in token_spans("ред3")] == ["ред", "3"]
    keys = set(word_keys("ред3"))
    assert {"w:ред", "w:3"} <= keys


def test_a_short_number_is_a_word_form_and_a_long_one_a_number_term():
    assert token_forms("3") == ("3",) and token_forms("12") == ("12",)
    assert token_forms("123") == ()
    keys = set(word_keys("page 7 of invoice 12345"))
    assert "w:7" in keys and "n:12345" in keys and "w:12345" not in keys


def test_tokens_split_like_name_keys():
    from src.domain.drive_name_keys import _tokens
    for text in ("Нью-Йорк", "IMG_2210.jpg", "Crème brûlée 2024", "myFileName v2", "Київ — «Львів»", "Пётр's ʼдім"):
        assert [t for t, _, _ in token_spans(text)] == _tokens(text), text


def test_token_spans_point_into_the_original_text():
    text = "Hotel in Barcelona, 3 nights"
    for token, start, end in token_spans(text):
        assert text[start:end].casefold() == token


def test_symbols_separate_tokens():
    assert [t for t, _, _ in token_spans("100€ total")] == ["100", "total"]
