"""
Unit tests for LLM response parser.
"""
import pytest
from src.utils.llm_response_parser import parse_llm_response
from src.domain.messaging import RichContent

def test_parse_plain_text():
    raw = "Just a simple text response."
    user_text, summary, rich = parse_llm_response(raw)
    
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
    user_text, summary, rich = parse_llm_response(raw)
    
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
    user_text, summary, rich = parse_llm_response(raw)
    
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
    user_text, summary, rich = parse_llm_response(raw)
    
    assert user_text == ""
    assert summary == "Showed table"
    assert isinstance(rich, RichContent)

def test_parse_invalid_json_fallback():
    raw = '{"full_response": "Broken JSON...'
    user_text, summary, rich = parse_llm_response(raw)
    
    # Should fallback to returning raw text as user_text
    assert user_text == raw
    assert summary is None
    assert rich is None

def test_parse_empty_input():
    user_text, summary, rich = parse_llm_response("")
    assert user_text == ""
    assert summary is None
    assert rich is None
