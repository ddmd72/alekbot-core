"""`summarizer_turns` keeps what the page knew about each entry: an "Ask Lelik" block is an `ask` turn, a heard line and
Lelik's line after it an `exchange` that says whether Lelik was cut off (VOICE_CALL_LIVE_TEXT_RFC §4.11)."""
import json

from src.domain.call_text_file import (
    TURN_KIND_ASK,
    TURN_KIND_EXCHANGE,
    CallTextEntry,
    parse_call_text_request,
    summarizer_turns,
)


def _body(mode, entries):
    return {"target": "chat", "mode": mode, "duration_s": 61, "entries": entries}


def test_each_entry_shape_gives_its_turn_fields():
    request = parse_call_text_request(_body("interpreter", [
        {"source": "Is this Albergo Immaginario?", "translation": "È questo l'Albergo Immaginario?"},
        {"source": "We have a booking under RM-55120.", "translation": "Abbiamo una prenotazione con", "interrupted": True},
        {"question": "How do I say 'where is Café Andorinha'?", "answer": "Onde fica o Café Andorinha?"},
    ]))
    assert summarizer_turns(request.entries) == [
        {"kind": TURN_KIND_EXCHANGE, "request_text": "Is this Albergo Immaginario?",
         "response_text": "È questo l'Albergo Immaginario?", "interrupted": False},
        {"kind": TURN_KIND_EXCHANGE, "request_text": "We have a booking under RM-55120.", "response_text": "Abbiamo una prenotazione con",
         "interrupted": True},
        {"kind": TURN_KIND_ASK, "request_text": "How do I say 'where is Café Andorinha'?",
         "response_text": "Onde fica o Café Andorinha?"},
    ]


def test_a_lesson_is_all_exchanges():
    request = parse_call_text_request(_body("tutor", [
        {"source": "Ayer voy a Coimbra", "translation": "Ah, ayer fuiste a Coimbra. ¿Con quién fuiste?"},
        {"source": "", "translation": "¿Y qué tal el", "interrupted": True},
    ]))
    turns = summarizer_turns(request.entries)
    assert [t["kind"] for t in turns] == [TURN_KIND_EXCHANGE, TURN_KIND_EXCHANGE]
    assert [t["interrupted"] for t in turns] == [False, True]


def test_the_kinds_are_distinct_and_the_turns_are_json():
    assert TURN_KIND_ASK != TURN_KIND_EXCHANGE
    turns = summarizer_turns((CallTextEntry("a", "b", interrupted=True), CallTextEntry("q", "r", lelik=True)))
    assert json.loads(json.dumps(turns)) == turns
    assert "interrupted" not in turns[1]
