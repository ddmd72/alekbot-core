"""`parse_call_text_request` and `summarizer_turns`: both entry shapes, every refusal, the entry cap."""
import pytest

from src.domain.call_text_file import (
    MAX_CALL_DURATION_S,
    MAX_CALL_TEXT_ENTRIES,
    CallTextEntry,
    CallTextTooLarge,
    parse_call_text_request,
    summarizer_turns,
)
from src.domain.voice_mode import VoiceMode

PAIR = {"source": "We have a booking under RM-55120.", "translation": "Abbiamo una prenotazione con il codice RM-55120."}
BLOCK = {"question": "How do I say it?", "answer": "Quisiera un café con leche"}


def body(**changes):
    base = {"target": "drive", "mode": "interpreter", "duration_s": 61.5, "entries": [PAIR]}
    base.update(changes)
    return base


def test_a_translation_pair_parses():
    req = parse_call_text_request(body(entries=[{**PAIR, "interrupted": True}]))
    assert req.target == "drive" and req.mode is VoiceMode.INTERPRETER and req.duration_s == 61.5
    assert req.entries == (CallTextEntry(PAIR["source"], PAIR["translation"], interrupted=True),)


def test_a_lelik_block_parses_and_is_marked():
    req = parse_call_text_request(body(target="chat", mode="tutor", entries=[BLOCK]))
    assert req.entries == (CallTextEntry(BLOCK["question"], BLOCK["answer"], lelik=True),)


def test_an_empty_side_is_allowed():
    req = parse_call_text_request(body(entries=[{"source": "", "translation": "ok"}]))
    assert req.entries[0].source == ""


@pytest.mark.parametrize("bad", [
    None, [], "text",
    body(target="email"), body(target=None),
    body(mode="lelik"), body(mode="nope"), body(mode=None),
    body(duration_s="5"), body(duration_s=True), body(duration_s=-1), body(duration_s=MAX_CALL_DURATION_S + 1),
    body(duration_s=None),
    body(entries=[]), body(entries="x"), body(entries={}),
    body(entries=["x"]),
    body(entries=[{"source": "a"}]),
    body(entries=[{**PAIR, "extra": 1}]),
    body(entries=[{**PAIR, **BLOCK}]),
    body(entries=[{"source": 1, "translation": "b"}]),
    body(entries=[{"source": "a", "translation": None}]),
    body(entries=[{**PAIR, "interrupted": "yes"}]),
    body(entries=[{**PAIR, "interrupted": 1}]),
    body(entries=[{"question": "q"}]),
    body(entries=[{"question": 1, "answer": "a"}]),
])
def test_a_malformed_body_is_refused(bad):
    with pytest.raises(ValueError) as caught:
        parse_call_text_request(bad)
    assert not isinstance(caught.value, CallTextTooLarge)


def test_the_duration_bounds_are_inclusive():
    assert parse_call_text_request(body(duration_s=0)).duration_s == 0.0
    assert parse_call_text_request(body(duration_s=MAX_CALL_DURATION_S)).duration_s == MAX_CALL_DURATION_S


def test_the_entry_cap_is_inclusive():
    assert len(parse_call_text_request(body(entries=[PAIR] * MAX_CALL_TEXT_ENTRIES)).entries) == MAX_CALL_TEXT_ENTRIES


def test_one_entry_over_the_cap_is_too_large():
    with pytest.raises(CallTextTooLarge):
        parse_call_text_request(body(entries=[PAIR] * (MAX_CALL_TEXT_ENTRIES + 1)))


def test_the_summarizer_reads_what_was_said_and_the_reply():
    entries = (CallTextEntry("a", "b"), CallTextEntry("q", "r", lelik=True))
    assert summarizer_turns(entries) == [{"kind": "exchange", "request_text": "a", "response_text": "b",
                                          "interrupted": False},
                                         {"kind": "ask", "request_text": "q", "response_text": "r"}]
