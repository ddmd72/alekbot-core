from src.domain.drive_name_keys import covers_query, name_keys, name_match, query_name_keys, query_token_groups
from src.domain.drive_index_params import NAME_QUERY_KEYS_MAX


def test_splits_on_separators_case_and_digit_boundaries():
    keys = name_keys("IMG_4912.JPG")
    assert {"w:img", "w:4912", "w:jpg"} <= set(keys)
    assert {"w:my", "w:report", "w:2026"} <= set(name_keys("MyReport2026.docx"))


def test_nfc_and_casefold():
    import unicodedata
    assert name_keys(unicodedata.normalize("NFD", "Ідеї.md")) == name_keys(unicodedata.normalize("NFC", "Ідеї.md"))
    assert "w:ідеї" in name_keys("ІДЕЇ.md")


def test_cyrillic_name_gets_latin_transliteration():
    keys = set(name_keys("Договор_ред3.docx"))
    assert {"w:договор", "w:dogovor", "w:ред", "w:red", "w:3"} <= keys


def test_latin_name_gets_cyrillic_transliteration():
    assert "w:договор" in set(name_keys("dogovor.pdf"))


def test_trigrams_for_tokens_of_three_or_more():
    keys = set(name_keys("Contrato.pdf"))
    assert {"t:con", "t:ont", "t:ntr", "t:tra", "t:rat", "t:ato"} <= keys
    assert not any(k.startswith("t:") and len(k) != 5 for k in keys)


def test_query_keys_order_whole_then_translit_then_trigrams_and_cap():
    q = query_name_keys("dogovor red3")
    whole = [k for k in q if k.startswith("w:")]
    assert q[: len(whole)] == tuple(whole)       # whole tokens (incl. translits) first
    assert q.index("w:dogovor") < q.index("w:договор")
    long_q = query_name_keys(" ".join(f"word{i}abc" for i in range(40)))
    assert len(long_q) == NAME_QUERY_KEYS_MAX


def test_match_requires_whole_token_or_three_trigrams():
    rec = name_keys("Contrato de alquiler 2026.pdf")
    assert name_match(rec, query_name_keys("alquiler")) is not None          # whole token
    assert name_match(rec, query_name_keys("alquilr")) is not None           # typo: shares alq, lqu, qui, uil
    assert name_match(rec, query_name_keys("xyz")) is None
    assert name_match(name_keys("Budget.ods"), query_name_keys("bud")) is None   # one trigram, no whole token


def test_match_counts_shared_keys():
    rec = name_keys("Договор_ред3.docx")
    assert name_match(rec, query_name_keys("dogovor red3")) > name_match(rec, query_name_keys("dogovor"))


def test_query_token_groups_pair_each_token_with_its_transliteration():
    assert query_token_groups("dogovor red3") == (frozenset({"w:dogovor", "w:договор"}), frozenset({"w:red", "w:ред"}),
                                                  frozenset({"w:3"}))


def test_cover_needs_every_token_in_some_script():
    rec = name_keys("Договор_ред3.docx")
    assert covers_query(rec, query_token_groups("dogovor red3")) is True
    assert covers_query(rec, query_token_groups("ред3")) is True
    assert covers_query(rec, query_token_groups("договор ред4")) is False
    assert covers_query(name_keys("IMG_4912.jpg"), query_token_groups("IMG 4912")) is True
    assert covers_query(rec, ()) is False
