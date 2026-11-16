"""The header form of Stage 3's email candidates (email index v2 §6.7, slice 9b) in `CONSOLIDATION_EMAIL_CANDIDATES`:
every key `header_candidate` can send is named in the header sentence, which tells it apart from a card by the missing
doc_type, says its attachments are unread, and routes a date its subject or a file name writes through the time rule;
the time rule and the about rule each say how a header is read; the token no longer describes the one-sentence form."""
import re

from src.domain.email_memory_candidate import HEADER_CANDIDATE_KEYS
from tests.support import prompt_vocabulary as pv

BODY = pv.ser.token_from_file(pv.token_path("CONSOLIDATION_EMAIL_CANDIDATES").read_text(encoding="utf-8"))["content"]


def _line(key: str) -> str:
    (line,) = [ln for ln in BODY.splitlines() if ln.strip().startswith(f"{key}: \"")]
    return line


def test_the_header_sentence_names_every_key_and_how_it_differs_from_a_card():
    header = _line("header")
    for key in HEADER_CANDIDATE_KEYS:
        assert re.search(rf"\b{key}\b", header)
    assert "no doc_type" in header and "read false" in header and "no times" in header


def test_the_time_and_about_rules_say_how_a_header_is_read():
    assert "In a header, a date its subject or an attachment's name writes takes the place of times" in _line("time_rule")
    assert "A header has no about" in _line("about")


def test_the_one_sentence_form_is_gone():
    lowered = BODY.lower()
    for gone in ("one-sentence", "a fact key", "category and tags", "line:"):
        assert gone not in lowered
