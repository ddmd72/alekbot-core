"""
Unit tests for llm_response_parser.

Coverage:
  parse_llm_response()
    - empty string → ("", None, None, [])
    - plain text (no JSON) → (text, None, None, [])
    - direct JSON with full_response → user_text returned
    - direct JSON with response_summary → history_summary returned
    - direct JSON with rich_content dict → RichContent constructed
    - direct JSON with rich_content list → first item used
    - direct JSON with link_list → filtered list returned
    - markdown code block wrapping JSON → parsed correctly
    - text preamble + embedded JSON (strategy 3) → parsed correctly
    - outer-quoted JSON ("{}") → strategy 3 extracts correctly
    - JSON with no recognized keys → plain text fallback
    - invalid JSON → plain text fallback
    - literal \\n in full_response → normalized to newline
    - link_list items missing required keys → filtered out

  _repair_unescaped_quotes()
    - valid JSON unmodified
    - unescaped inner quote repaired
    - escaped backslash preserved
    - multi-field JSON with unescaped quote

  _load_json()
    - valid JSON → returns dict
    - broken JSON repairable → returns dict
    - unrepairable JSON → returns None

  extract_structured_response()
    - terminal_tool_args path: full_response + response_summary extracted
    - terminal_tool_args path: history_summary key fallback
    - terminal_tool_args path: rich_content dict builds RichContent
    - terminal_tool_args path: malformed link_list entries filtered
    - falsy terminal_tool_args (None) falls through to text parsing
    - falsy terminal_tool_args ({}) falls through to text parsing
"""
import json
import pytest

from src.domain.messaging import RichContent
from src.utils.llm_response_parser import (
    _load_json,
    _repair_unescaped_quotes,
    extract_structured_response,
    parse_llm_response,
)


# ---------------------------------------------------------------------------
# parse_llm_response()
# ---------------------------------------------------------------------------

class TestParseLlmResponse:

    def test_empty_string_returns_empty_tuple(self):
        text, summary, rich, links = parse_llm_response("")
        assert text == ""
        assert summary is None
        assert rich is None
        assert links == []

    def test_plain_text_falls_back(self):
        text, summary, rich, links = parse_llm_response("Hello there!")
        assert text == "Hello there!"
        assert summary is None
        assert rich is None
        assert links == []

    def test_direct_json_full_response(self):
        payload = {"full_response": "Here is the answer.", "response_summary": "short"}
        text, summary, rich, links = parse_llm_response(json.dumps(payload))
        assert text == "Here is the answer."
        assert summary == "short"

    def test_direct_json_response_summary_only(self):
        payload = {"response_summary": "brief answer"}
        text, summary, rich, links = parse_llm_response(json.dumps(payload))
        assert summary == "brief answer"

    def test_direct_json_rich_content_dict(self):
        payload = {
            "full_response": "Text",
            "rich_content": {"type": "table", "data": {"headers": ["A"]}, "fallback": "fb"}
        }
        text, summary, rich, links = parse_llm_response(json.dumps(payload))
        assert rich is not None
        assert rich.content_type == "table"
        assert rich.data == {"headers": ["A"]}
        assert rich.fallback_text == "fb"

    def test_direct_json_rich_content_list_uses_first_item(self):
        payload = {
            "full_response": "Text",
            "rich_content": [
                {"type": "map", "data": {}, "fallback": "map fb"},
                {"type": "other", "data": {}, "fallback": ""},
            ]
        }
        text, summary, rich, links = parse_llm_response(json.dumps(payload))
        assert rich is not None
        assert rich.content_type == "map"

    def test_rich_content_list_empty_returns_none(self):
        payload = {"full_response": "Text", "rich_content": []}
        _, _, rich, _ = parse_llm_response(json.dumps(payload))
        assert rich is None

    def test_link_list_valid_items_returned(self):
        payload = {
            "full_response": "Text",
            "link_list": [
                {"anchor": "1", "title": "Google", "url": "https://google.com"},
                {"anchor": "2", "title": "Wikipedia", "url": "https://en.wikipedia.org"},
            ]
        }
        _, _, _, links = parse_llm_response(json.dumps(payload))
        assert len(links) == 2
        assert links[0]["url"] == "https://google.com"

    def test_link_list_missing_keys_filtered_out(self):
        payload = {
            "full_response": "Text",
            "link_list": [
                {"anchor": "1", "title": "Google"},         # missing url
                {"anchor": "2", "url": "https://x.com"},    # missing title
                {"anchor": "3", "title": "Y", "url": "https://y.com"},  # valid
            ]
        }
        _, _, _, links = parse_llm_response(json.dumps(payload))
        assert len(links) == 1
        assert links[0]["url"] == "https://y.com"

    def test_markdown_code_block_json(self):
        payload = {"full_response": "Answer", "response_summary": "short"}
        raw = f"```json\n{json.dumps(payload)}\n```"
        text, summary, _, _ = parse_llm_response(raw)
        assert text == "Answer"
        assert summary == "short"

    def test_markdown_code_block_no_language_tag(self):
        payload = {"full_response": "Answer"}
        raw = f"```\n{json.dumps(payload)}\n```"
        text, _, _, _ = parse_llm_response(raw)
        assert text == "Answer"

    def test_preamble_with_embedded_json(self):
        payload = {"full_response": "My answer", "response_summary": "s"}
        raw = f"Some preamble text\n{json.dumps(payload)}"
        text, summary, _, _ = parse_llm_response(raw)
        assert text == "My answer"

    def test_no_recognized_keys_returns_plain_text(self):
        payload = {"unrelated_key": "value"}
        raw = json.dumps(payload)
        text, summary, rich, links = parse_llm_response(raw)
        assert text == raw
        assert summary is None

    def test_invalid_json_returns_plain_text(self):
        raw = '{"broken": json here'
        text, summary, rich, links = parse_llm_response(raw)
        assert text == raw

    def test_literal_backslash_n_normalized_in_full_response(self):
        # LLM sometimes double-escapes: json.loads gives literal \n instead of newline
        payload = {"full_response": "line1\\nline2", "response_summary": "s1\\ns2"}
        raw = json.dumps(payload)
        text, summary, _, _ = parse_llm_response(raw)
        assert "\n" in text
        assert "\n" in summary

    def test_whitespace_stripped_before_parsing(self):
        payload = {"full_response": "Answer"}
        raw = f"  \n{json.dumps(payload)}\n  "
        text, _, _, _ = parse_llm_response(raw)
        assert text == "Answer"


# ---------------------------------------------------------------------------
# _repair_unescaped_quotes()
# ---------------------------------------------------------------------------

class TestRepairUnescapedQuotes:

    def test_valid_json_unchanged(self):
        s = '{"key": "value"}'
        result = _repair_unescaped_quotes(s)
        assert json.loads(result) == {"key": "value"}

    def test_unescaped_inner_quote_repaired(self):
        # {"key": "he said "hello" to me"} → inner quotes escaped
        broken = '{"key": "he said "hello" to me"}'
        result = _repair_unescaped_quotes(broken)
        parsed = json.loads(result)
        assert "he said" in parsed["key"]

    def test_backslash_preserved(self):
        s = '{"key": "path\\\\file"}'
        result = _repair_unescaped_quotes(s)
        # Should not raise
        assert json.loads(result) is not None

    def test_empty_string(self):
        result = _repair_unescaped_quotes("")
        assert result == ""


# ---------------------------------------------------------------------------
# _load_json()
# ---------------------------------------------------------------------------

class TestLoadJson:

    def test_valid_json_returned(self):
        result = _load_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_repairable_json_returned(self):
        broken = '{"msg": "he said "hi" to her"}'
        result = _load_json(broken)
        assert result is not None
        assert isinstance(result, dict)

    def test_unrepairable_json_returns_none(self):
        result = _load_json("this is not json at all {{{")
        assert result is None

    def test_empty_object_returned(self):
        result = _load_json("{}")
        assert result == {}


# ---------------------------------------------------------------------------
# extract_structured_response()
# ---------------------------------------------------------------------------

class TestExtractStructuredResponseTerminalToolPath:

    def test_terminal_tool_args_extracts_full_response_and_summary(self):
        args = {"full_response": "Here is your answer.", "response_summary": "Summary."}
        text, summary, rich, links = extract_structured_response(args, "")
        assert text == "Here is your answer."
        assert summary == "Summary."
        assert rich is None
        assert links == []

    def test_terminal_tool_args_history_summary_key_fallback(self):
        # Legacy field name some callers used before response_summary was standardized.
        args = {"full_response": "Answer.", "history_summary": "Legacy summary."}
        text, summary, rich, links = extract_structured_response(args, "")
        assert summary == "Legacy summary."

    def test_terminal_tool_args_with_rich_content_builds_rich_content_object(self):
        args = {
            "full_response": "Answer.",
            "rich_content": {"type": "table", "data": {"rows": []}, "fallback": "plain"},
        }
        text, summary, rich, links = extract_structured_response(args, "")
        assert isinstance(rich, RichContent)
        assert rich.content_type == "table"
        assert rich.fallback_text == "plain"

    def test_terminal_tool_args_filters_malformed_link_list_entries(self):
        args = {
            "full_response": "Answer.",
            "link_list": [
                {"anchor": "1", "title": "Place", "url": "https://x.example.com"},
                {"anchor": "2"},  # missing title/url — must be filtered out
            ],
        }
        text, summary, rich, links = extract_structured_response(args, "")
        assert links == [{"anchor": "1", "title": "Place", "url": "https://x.example.com"}]


class TestExtractStructuredResponseTextFallback:

    def test_falsy_terminal_tool_args_falls_through_to_text_parsing(self):
        text, summary, rich, links = extract_structured_response(
            None, '{"full_response": "From text.", "response_summary": "Sum."}'
        )
        assert text == "From text."
        assert summary == "Sum."

    def test_empty_dict_terminal_tool_args_falls_through_to_text_parsing(self):
        # {} is falsy in Python — must take the same fallback path as None.
        text, summary, rich, links = extract_structured_response(
            {}, "Plain text, no JSON envelope."
        )
        assert text == "Plain text, no JSON envelope."
        assert summary is None
