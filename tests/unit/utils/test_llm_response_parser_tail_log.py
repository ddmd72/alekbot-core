"""The first-object fallback logs only the length of the trailing text, never the text (RFC §4.6)."""
import logging

from src.utils.llm_response_parser import extract_structured_response

MARKER = "ZXQ-TRAILING-MARKER"


def test_trailing_text_not_logged(caplog):
    text = '{"full_response": "hello", "response_summary": "r"}} ' + MARKER + " more"
    with caplog.at_level(logging.DEBUG):
        extract_structured_response(None, text)
    msgs = [r.getMessage() for r in caplog.records if "first_object" in r.getMessage()]
    assert msgs
    assert all(MARKER not in m and "tail_len=" in m for m in msgs)
