"""chat_since_lines is the body of render_chat_since (SYSTEM_TURN_FINGERPRINT_RFC §4.3)."""
from src.domain.llm import Message, MessagePart
from src.domain.turn_clock import MEANWHILE_HEADER, chat_since_lines, render_chat_since


def _msgs():
    return [
        Message(role="user", parts=[MessagePart(text="also check Tuesday")]),
        Message(role="model", parts=[MessagePart(text="on it")]),
        Message(role="user", parts=[MessagePart(text="  ")]),
    ]


def test_render_is_header_plus_lines():
    msgs = _msgs()
    assert render_chat_since(msgs) == "\n".join([MEANWHILE_HEADER, *chat_since_lines(msgs)])


def test_lines_skip_empty_and_label_roles():
    assert chat_since_lines(_msgs()) == ["- user: also check Tuesday", "- you: on it"]
