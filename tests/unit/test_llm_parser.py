"""
Unit tests for LLM response parser.
"""
import pytest
from src.utils.llm_response_parser import parse_llm_response
from src.domain.messaging import RichContent

def test_parse_plain_text():
    raw = "Just a simple text response."
    user_text, summary, rich, link_list = parse_llm_response(raw)
    
    assert user_text == "Just a simple text response."
    assert summary is None
    assert rich is None

def test_parse_valid_json_full():
    raw = '''{
        "full_response": "Here is the detail.",
        "response_summary": "Summary text",
        "rich_content": {
            "type": "weather",
            "data": {"temp": 20},
            "fallback": "Sunny 20C"
        }
    }'''
    user_text, summary, rich, link_list = parse_llm_response(raw)
    
    assert user_text == "Here is the detail."
    assert summary == "Summary text"
    assert isinstance(rich, RichContent)
    assert rich.content_type == "weather"
    assert rich.data == {"temp": 20}
    assert rich.fallback_text == "Sunny 20C"

def test_parse_json_markdown_block():
    raw = '''```json
    {
        "full_response": "Markdown block test"
    }
    ```'''
    user_text, summary, rich, link_list = parse_llm_response(raw)
    
    assert user_text == "Markdown block test"
    assert summary is None
    assert rich is None

def test_parse_rich_only():
    raw = '''{
        "full_response": "",
        "rich_content": {
            "type": "table",
            "data": {},
            "fallback": "Table data"
        },
        "response_summary": "Showed table"
    }'''
    user_text, summary, rich, link_list = parse_llm_response(raw)
    
    assert user_text == ""
    assert summary == "Showed table"
    assert isinstance(rich, RichContent)

def test_parse_invalid_json_fallback():
    raw = '{"full_response": "Broken JSON...'
    user_text, summary, rich, link_list = parse_llm_response(raw)
    
    # Should fallback to returning raw text as user_text
    assert user_text == raw
    assert summary is None
    assert rich is None

def test_parse_empty_input():
    user_text, summary, rich, link_list = parse_llm_response("")
    assert user_text == ""
    assert summary is None
    assert rich is None


def test_parse_text_preamble_before_json():
    # Model outputs reasoning text followed by the JSON envelope (no code block).
    raw = 'Ось відповідь для користувача.\n{"full_response": "actual answer", "response_summary": "summary"}'
    user_text, summary, rich, link_list = parse_llm_response(raw)
    assert user_text == "actual answer"
    assert summary == "summary"
    assert rich is None


def test_parse_outer_quoted_json():
    # Model wraps the entire JSON in outer quotes (unescaped inner quotes — not valid JSON string,
    # but a literal model output artifact): "{\n  "full_response": "answer"\n}"
    raw = '"{\n  "full_response": "quoted answer",\n  "response_summary": null\n}"'
    user_text, summary, rich, link_list = parse_llm_response(raw)
    assert user_text == "quoted answer"
    assert summary is None
    assert rich is None


def test_parse_link_list():
    raw = '''{
        "full_response": "Bar Casa Vio [1] is great.",
        "response_summary": "Great place",
        "rich_content": null,
        "link_list": [
            {"anchor": "1", "title": "Bar Casa Vio", "url": "https://maps.google.com/?cid=123"}
        ]
    }'''
    user_text, summary, rich, link_list = parse_llm_response(raw)
    assert user_text == "Bar Casa Vio [1] is great."
    assert len(link_list) == 1
    assert link_list[0]["anchor"] == "1"
    assert link_list[0]["title"] == "Bar Casa Vio"
    assert link_list[0]["url"] == "https://maps.google.com/?cid=123"


def test_parse_link_list_invalid_items_filtered():
    raw = '''{
        "full_response": "Some text",
        "response_summary": "Summary",
        "rich_content": null,
        "link_list": [
            {"anchor": "1", "title": "Valid", "url": "https://example.com"},
            {"anchor": "2", "title": "Missing url"},
            "not an object"
        ]
    }'''
    user_text, summary, rich, link_list = parse_llm_response(raw)
    assert len(link_list) == 1
    assert link_list[0]["anchor"] == "1"
