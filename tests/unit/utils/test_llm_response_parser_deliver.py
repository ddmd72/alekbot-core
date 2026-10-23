"""
Tests for the deliver shape, the first-object recovery and the one-line parse log
of src/utils/llm_response_parser.py (decision: claude_deliver_response_string_tool).
"""
import json
import logging

import pytest

from src.domain.messaging import RichContent
from src.utils.llm_response_parser import extract_structured_response, parse_llm_response

OBJ = '{"full_response": "Hello", "response_summary": "Sum"}'


def _records(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "llm_response_parse"]


def _one(caplog):
    recs = _records(caplog)
    assert len(recs) == 1
    return recs[0]


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


class TestFirstObjectRecovery:

    @pytest.mark.parametrize("tail", ['"}', "}", ', "link_list": []}'])
    def test_trailing_chars_after_complete_object(self, tail, caplog):
        text, summary, rich, links = parse_llm_response(OBJ + tail)
        assert (text, summary, rich, links) == ("Hello", "Sum", None, [])
        rec = _one(caplog)
        assert rec.outcome == "first_object"
        assert rec.levelno == logging.INFO

    def test_first_object_logs_tail_not_text(self, caplog):
        parse_llm_response(OBJ + "}" + "x" * 200)
        msg = _one(caplog).getMessage()
        assert "tail=" in msg and "x" * 61 not in msg and "Hello" not in msg

    def test_control_characters_inside_string_are_accepted(self, caplog):
        raw = '{"full_response": "line1\nline2\tend", "response_summary": "s"}'
        text, summary, _, _ = parse_llm_response(raw)
        assert text == "line1\nline2\tend"
        rec = _one(caplog)
        assert (rec.outcome, rec.via) == ("parsed", "direct")

    def test_fence_with_trailing_chars_inside(self, caplog):
        raw = "```json\n" + OBJ + '"}\n```'
        text, summary, _, _ = parse_llm_response(raw)
        assert (text, summary) == ("Hello", "Sum")
        assert _one(caplog).outcome == "first_object"

    def test_prose_with_non_envelope_brace_is_unparseable(self, caplog):
        raw = 'Use the set {"a": 1} like so.'
        assert parse_llm_response(raw) == (raw, None, None, [])
        assert _one(caplog).outcome == "unparseable"

    def test_unclosed_object_is_unparseable(self, caplog):
        raw = '{"full_response": "never closed'
        assert parse_llm_response(raw)[0] == raw
        assert _one(caplog).outcome == "unparseable"

    def test_anchor_chain_wins_over_first_object(self, caplog):
        # A non-envelope JSON preamble, then the envelope later: today's anchor
        # strategy must still pick the envelope (first_object runs only after failure).
        raw = 'see {"note": 1}\nresult: ' + OBJ
        text, summary, _, _ = parse_llm_response(raw)
        assert (text, summary) == ("Hello", "Sum")
        rec = _one(caplog)
        assert (rec.outcome, rec.via) == ("parsed", "anchor")


class TestParseLog:

    def test_direct(self, caplog):
        parse_llm_response(OBJ)
        rec = _one(caplog)
        assert (rec.source, rec.outcome, rec.via) == ("text", "parsed", "direct")
        assert "Hello" not in rec.getMessage()

    def test_fence(self, caplog):
        parse_llm_response("```json\n" + OBJ + "\n```")
        rec = _one(caplog)
        assert (rec.outcome, rec.via) == ("parsed", "fence")

    def test_anchor(self, caplog):
        parse_llm_response("preamble " + OBJ)
        rec = _one(caplog)
        assert (rec.outcome, rec.via) == ("parsed", "anchor")

    def test_repaired(self, caplog):
        parse_llm_response('{"full_response": "He said "hi" ok", "response_summary": "s"}')
        assert _one(caplog).outcome == "repaired"

    def test_empty_input_logs_unparseable(self, caplog):
        assert parse_llm_response("") == ("", None, None, [])
        rec = _one(caplog)
        assert (rec.source, rec.outcome) == ("text", "unparseable")

    def test_parsed_object_without_envelope_key(self, caplog):
        raw = '{"foo": "bar"}'
        assert parse_llm_response(raw) == (raw, None, None, [])
        assert _one(caplog).outcome == "unparseable"

    def test_text_unparseable_is_info(self, caplog):
        parse_llm_response("just words")
        rec = _one(caplog)
        assert rec.levelno == logging.INFO and rec.source == "text"

    def test_deliver_unparseable_nonempty_is_warning(self, caplog):
        parse_llm_response("just words", source="deliver")
        rec = _one(caplog)
        assert rec.levelno == logging.WARNING and rec.source == "deliver"

    def test_deliver_unparseable_empty_is_error(self, caplog):
        parse_llm_response("", source="deliver")
        rec = _one(caplog)
        assert rec.levelno == logging.ERROR and rec.source == "deliver"

    def test_deliver_parsed_is_info(self, caplog):
        parse_llm_response(OBJ, source="deliver")
        rec = _one(caplog)
        assert rec.levelno == logging.INFO and rec.source == "deliver"


class TestDeliverShape:

    def test_string_with_full_envelope(self, caplog):
        env = {
            "full_response": "Table below",
            "response_summary": "A table",
            "rich_content": {"type": "table", "data": {"rows": [{"cells": ["a", "b"]}]}, "fallback": "a|b"},
            "link_list": [
                {"anchor": "1", "title": "T", "url": "https://x.example.com"},
                {"anchor": "2"},
            ],
        }
        text, summary, rich, links = extract_structured_response({"response": json.dumps(env)}, "")
        assert (text, summary) == ("Table below", "A table")
        assert isinstance(rich, RichContent)
        assert rich.content_type == "table"
        assert rich.data == {"rows": [{"cells": ["a", "b"]}]}
        assert links == [{"anchor": "1", "title": "T", "url": "https://x.example.com"}]
        rec = _one(caplog)
        assert (rec.source, rec.outcome, rec.via) == ("deliver", "parsed", "direct")

    def test_double_escaped_newlines_normalised(self):
        raw = '{"full_response": "a\\\\nb", "response_summary": "c\\\\nd"}'
        text, summary, _, _ = extract_structured_response({"response": raw}, "")
        assert (text, summary) == ("a\nb", "c\nd")

    def test_unescaped_inner_quotes_repaired(self, caplog):
        raw = '{"full_response": "He said "hi" ok", "response_summary": "s"}'
        text, _, _, _ = extract_structured_response({"response": raw}, "")
        assert text == 'He said "hi" ok'
        rec = _one(caplog)
        assert (rec.source, rec.outcome) == ("deliver", "repaired")

    def test_list_rich_content_takes_first_item(self):
        raw = json.dumps({
            "full_response": "x",
            "rich_content": [{"type": "table", "data": {"rows": []}, "fallback": "f"}, {"type": "other"}],
        })
        _, _, rich, _ = extract_structured_response({"response": raw}, "")
        assert rich is not None and rich.content_type == "table"

    def test_dict_response_with_envelope(self, caplog):
        args = {"response": {"full_response": "Hi", "response_summary": "S", "link_list": []}}
        assert extract_structured_response(args, "") == ("Hi", "S", None, [])
        rec = _one(caplog)
        assert (rec.source, rec.outcome, rec.via) == ("deliver", "parsed", "object")

    def test_dict_response_without_envelope_keys(self, caplog):
        assert extract_structured_response({"response": {"foo": 1}}, "") == ("", None, None, [])
        rec = _one(caplog)
        assert (rec.outcome, rec.levelno) == ("unparseable", logging.ERROR)

    @pytest.mark.parametrize("bad", [None, [1, 2], 7])
    def test_non_string_non_dict_response(self, bad, caplog):
        assert extract_structured_response({"response": bad}, "") == ("", None, None, [])
        rec = _one(caplog)
        assert (rec.outcome, rec.levelno) == ("unparseable", logging.ERROR)

    def test_empty_string_response(self, caplog):
        assert extract_structured_response({"response": ""}, "") == ("", None, None, [])
        rec = _one(caplog)
        assert (rec.source, rec.outcome) == ("deliver", "unparseable")

    def test_plain_string_response_comes_back_as_text(self):
        text, summary, rich, links = extract_structured_response({"response": "Just prose."}, "")
        assert (text, summary, rich, links) == ("Just prose.", None, None, [])


class TestGrokShapeUnchanged:

    def test_response_key_with_full_response_takes_grok_path(self, caplog):
        args = {"full_response": "Grok text", "response_summary": "gs", "response": "ignored"}
        assert extract_structured_response(args, "") == ("Grok text", "gs", None, [])
        assert _records(caplog) == []

    @pytest.mark.parametrize("key,val", [
        ("response_summary", "s"), ("rich_content", {"type": "t"}),
        ("history_summary", "h"), ("link_list", []),
    ])
    def test_any_schema_key_beside_response_is_grok_shape(self, key, val, caplog):
        args = {"response": "ignored", key: val}
        text, _, _, _ = extract_structured_response(args, "")
        assert text == ""
        assert _records(caplog) == []


class TestTextBesideTerminalCall:

    def test_empty_full_response_uses_plain_text(self):
        args = {"full_response": "", "response_summary": "kept"}
        text, summary, _, _ = extract_structured_response(args, "Here you go")
        assert (text, summary) == ("Here you go", "kept")

    def test_text_that_is_envelope_gives_its_full_response(self):
        args = {"full_response": "  ", "response_summary": "kept"}
        text, summary, _, _ = extract_structured_response(args, '{"full_response": "From envelope"}')
        assert (text, summary) == ("From envelope", "kept")

    def test_nonempty_full_response_ignores_text(self):
        text, _, _, _ = extract_structured_response({"full_response": "Mine"}, "Other")
        assert text == "Mine"

    def test_deliver_shape_empty_answer_uses_text_and_logs(self, caplog):
        args = {"response": '{"full_response": "", "response_summary": "kept"}'}
        text, summary, _, _ = extract_structured_response(args, "Beside")
        assert (text, summary) == ("Beside", "kept")
        assert any(getattr(r, "event", None) == "terminal_text_used" for r in caplog.records)
        # parser called twice (deliver string, then the text): two parse lines, as per B3
        assert [r.source for r in _records(caplog)] == ["deliver", "text"]

    def test_blank_text_does_not_replace(self):
        text, _, _, _ = extract_structured_response({"full_response": ""}, "   ")
        assert text == ""


class TestNonStringFields:

    def test_grok_path_non_string_full_response_does_not_raise(self):
        assert extract_structured_response({"full_response": 5}, "") == (5, None, None, [])
        assert extract_structured_response({"full_response": ["a"]}, "ignored")[0] == ["a"]

    @pytest.mark.parametrize("raw", [
        {"full_response": 5},
        '{"full_response": 5}',
        {"full_response": ["a"]},
    ])
    def test_envelope_non_string_full_response_does_not_raise(self, raw):
        text, _, _, _ = extract_structured_response({"response": raw}, "")
        assert text == raw["full_response"] if isinstance(raw, dict) else text == 5

    @pytest.mark.parametrize("raw", [
        {"response_summary": 5},
        '{"response_summary": 5}',
        {"response_summary": ["a"]},
    ])
    def test_envelope_non_string_summary_does_not_raise(self, raw):
        _, summary, _, _ = extract_structured_response({"response": raw}, "")
        assert summary == (raw["response_summary"] if isinstance(raw, dict) else 5)


class TestTextBesideUnusableDeliver:

    @pytest.mark.parametrize("resp", [{"full_response": ""}, {"foo": 1}, True])
    def test_text_fills_user_text(self, resp):
        text, summary, rich, links = extract_structured_response({"response": resp}, "Beside")
        assert (text, summary, rich, links) == ("Beside", None, None, [])

    def test_beside_envelope_without_full_response_gives_empty_string(self):
        text, summary, _, _ = extract_structured_response(
            {"full_response": ""}, '{"response_summary": "s"}'
        )
        assert text == "" and summary is None
