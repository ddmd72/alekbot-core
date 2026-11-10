"""normalize_queries: whatever a client sent becomes one to four stripped phrases in the caller's order."""
import json

from src.domain.mcp import normalize_queries, queries_dropped


def test_a_list_keeps_its_order():
    phrases = ["Clara Moreira passport valid until 2026-12-09", "Clara Moreira passport renewal by 2026-12-01"]
    assert normalize_queries(phrases) == phrases


def test_a_json_encoded_array_is_decoded():
    phrases = ["Skoda Fabia 2015 insurance policy", "Skoda Fabia car service 2026-11-05"]
    assert normalize_queries(json.dumps(phrases)) == phrases


def test_one_string_is_one_phrase():
    assert normalize_queries("  Dr. Marta Quillen dentist check-up 2026-10-12 ") == [
        "Dr. Marta Quillen dentist check-up 2026-10-12"]


def test_empty_items_and_none_are_dropped():
    assert normalize_queries(["", "  ", None, "weekend in Coimbra with Clara Mar 6-8"]) == [
        "weekend in Coimbra with Clara Mar 6-8"]


def test_nothing_usable_gives_an_empty_list():
    assert normalize_queries(None) == []
    assert normalize_queries([]) == []
    assert normalize_queries("   ") == []


def test_only_the_first_four_are_kept_and_the_rest_counted():
    phrases = [f"phrase {i}" for i in range(6)]
    assert normalize_queries(phrases) == phrases[:4]
    assert queries_dropped(phrases) == 2
    assert queries_dropped(phrases[:4]) == 0
