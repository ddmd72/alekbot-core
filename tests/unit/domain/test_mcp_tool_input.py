"""
Normalization of `get_user_context` tool arguments.

These are the server side of "accept liberally, emit strictly": the MCP
input schema advertises plain `string` / `array<string>` types with no
constraints, and every shape a client might actually send is coerced
here instead of failing pydantic validation (a validation failure costs
a full extra round-trip with the calling model).

Covered:
- normalize_keywords: null, bare string, comma/semicolon strings,
  JSON-encoded arrays, casing/whitespace, embedded spaces, dedup, cap.
- normalize_phrase: null, whitespace, non-string scalars.
"""

import pytest

from src.domain.mcp import KEYWORDS_MAX, normalize_keywords, normalize_phrase


# ---------------------------------------------------------------------------
# normalize_keywords
# ---------------------------------------------------------------------------


class TestNormalizeKeywords:
    def test_none_becomes_empty_list(self):
        assert normalize_keywords(None) == []

    def test_empty_list_stays_empty(self):
        assert normalize_keywords([]) == []

    def test_empty_string_becomes_empty_list(self):
        assert normalize_keywords("") == []

    def test_bare_string_becomes_single_tag(self):
        assert normalize_keywords("mcp") == ["mcp"]

    def test_comma_separated_string_is_split(self):
        assert normalize_keywords("mcp, schema") == ["mcp", "schema"]

    def test_semicolon_separated_string_is_split(self):
        assert normalize_keywords("mcp; schema") == ["mcp", "schema"]

    def test_json_encoded_array_is_parsed(self):
        assert normalize_keywords('["mcp","schema"]') == ["mcp", "schema"]

    def test_json_encoded_array_is_trimmed_and_lowercased(self):
        assert normalize_keywords('["MCP", " Schema "]') == ["mcp", "schema"]

    def test_list_items_are_trimmed_and_lowercased(self):
        assert normalize_keywords(["MCP", " Schema "]) == ["mcp", "schema"]

    def test_item_containing_space_is_split_into_tags(self):
        assert normalize_keywords(["mcp schema", "connector"]) == [
            "mcp",
            "schema",
            "connector",
        ]

    def test_duplicates_are_removed_preserving_order(self):
        assert normalize_keywords(["schema", "mcp", "Schema"]) == ["schema", "mcp"]

    def test_excess_keywords_are_truncated_silently(self):
        many = [f"tag{i}" for i in range(KEYWORDS_MAX + 3)]

        result = normalize_keywords(many)

        assert result == [f"tag{i}" for i in range(KEYWORDS_MAX)]

    def test_blank_items_are_dropped(self):
        assert normalize_keywords(["mcp", "", "   ", "schema"]) == ["mcp", "schema"]

    def test_non_string_scalar_is_stringified(self):
        assert normalize_keywords(42) == ["42"]

    def test_json_scalar_string_is_stringified(self):
        assert normalize_keywords("42") == ["42"]


# ---------------------------------------------------------------------------
# normalize_phrase
# ---------------------------------------------------------------------------


class TestNormalizePhrase:
    def test_none_becomes_empty_string(self):
        assert normalize_phrase(None) == ""

    def test_whitespace_is_trimmed(self):
        assert normalize_phrase("  какие у меня проекты  ") == "какие у меня проекты"

    def test_blank_string_becomes_empty(self):
        assert normalize_phrase("   ") == ""

    def test_plain_string_is_unchanged(self):
        assert normalize_phrase("mcp connector schema") == "mcp connector schema"

    def test_non_string_scalar_is_stringified(self):
        assert normalize_phrase(42) == "42"

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_falsy_inputs_are_all_empty_string(self, value):
        assert normalize_phrase(value) == ""
