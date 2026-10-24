"""fact_embedding_texts: the shared embedding recipe for a fact."""

import json

from src.domain.fact_embedding import fact_embedding_texts


def test_order_is_text_tags_metadata():
    out = fact_embedding_texts("mother is Maria", ["family", "mother"], {"name": "Maria"})
    assert out == ["mother is Maria", "family mother", '{"name": "Maria"}']


def test_metadata_keeps_non_ascii():
    out = fact_embedding_texts("t", ["x"], {"ім'я": "Марія"})
    assert out[2] == json.dumps({"ім'я": "Марія"}, ensure_ascii=False)
    assert "Марія" in out[2]


def test_empty_or_none_tags_fall_back_to_text():
    assert fact_embedding_texts("t", [], {"a": 1})[1] == "t"
    assert fact_embedding_texts("t", None, {"a": 1})[1] == "t"


def test_empty_or_none_metadata_fall_back_to_text():
    assert fact_embedding_texts("t", ["a"], {})[2] == "t"
    assert fact_embedding_texts("t", ["a"], None)[2] == "t"


def test_system_tags_are_left_out_of_tags_text():
    out = fact_embedding_texts("t", ["family", "consolidated", "anchor", "mother"], {"a": 1})
    assert out[1] == "family mother"


def test_only_system_tags_fall_back_to_text():
    assert fact_embedding_texts("t", ["consolidated", "anchor"], {"a": 1})[1] == "t"


def test_remaining_tag_order_is_preserved():
    assert fact_embedding_texts("t", ["z", "consolidated", "a", "m"], None)[1] == "z a m"
