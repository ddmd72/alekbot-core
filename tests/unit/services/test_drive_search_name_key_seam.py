"""The name-key seam (USER_DRIVE_INDEXING_RFC §25.2.4: one normaliser on both sides; review rv_sv2_p5 M2): the search
tests a record's name keys v2 with the same whole keys the index writes — `w:`, `x:`, `n:` and `y:` all count as whole
tokens over the name_match floor, only trigrams (`t:`) need NAME_MIN_TRIGRAMS of them."""
from src.domain.drive_keys import name_keys_v2, name_match_v2, query_concepts
from src.domain.drive_tier_pool import concept_trigrams
from src.domain.drive_tiers import name_query_keys

from .drive_search_v2_fixture import V2World


def _query_keys(query):
    return [k for c in query_concepts(query).concepts for k in (*name_query_keys(c), *concept_trigrams(c))]


def test_a_digit_or_year_key_alone_is_a_whole_token_match():
    assert name_match_v2(name_keys_v2("Invoice 2025.pdf"), _query_keys("2025")) == 2       # n:2025, y:2025
    assert name_match_v2(name_keys_v2("IMG_4912.jpg"), _query_keys("4912")) == 1           # n:4912


def test_a_skeleton_alone_is_a_whole_token_match():
    assert name_match_v2(name_keys_v2("Barselona.txt"), ["x:barselon"]) == 1


def test_trigrams_alone_still_need_the_floor():
    keys = name_keys_v2("Barcelona.pdf")
    assert name_match_v2(keys, ["t:bar", "t:arc"]) is None
    assert name_match_v2(keys, ["t:bar", "t:arc", "t:rce"]) == 3


async def test_a_folder_search_finds_a_file_by_the_number_in_its_name():
    w = V2World()
    work = w.folder("Work")
    target = w.file("Invoice 2025.pdf", parent=work)
    w.file("Invoice 2024.pdf", parent=work)
    result = await w.search("2025", folder="Work")
    assert [c.item_id for c in result.cards if c.tier != "related"] == [target]
