"""`handoff_key`: the deterministic id of a handed-off batch (owner decision O2, P11-G2-3)."""
from src.domain.consolidation import handoff_key
from src.domain.llm import Message, MessagePart


def msg(text, created_at=1000.0, role="user", full_text=None):
    return Message(role=role, parts=[MessagePart(text=text, full_text=full_text)], created_at=created_at)


def test_the_same_messages_of_the_same_session_give_the_same_key():
    first = handoff_key("u1:C1", [msg("When is the car service?"), msg("On 2026-11-05.", 1001.0, "model")])
    again = handoff_key("u1:C1", [msg("When is the car service?"), msg("On 2026-11-05.", 1001.0, "model")])
    assert first == again and len(first) == 24 and int(first, 16) >= 0


def test_session_order_time_role_and_each_text_change_the_key():
    base = handoff_key("u1:C1", [msg("a"), msg("b", 1001.0)])
    variants = [
        handoff_key("u1:C2", [msg("a"), msg("b", 1001.0)]),
        handoff_key("u1:C1", [msg("b", 1001.0), msg("a")]),
        handoff_key("u1:C1", [msg("a", 1000.5), msg("b", 1001.0)]),
        handoff_key("u1:C1", [msg("a", role="model"), msg("b", 1001.0)]),
        handoff_key("u1:C1", [msg("a", full_text="a, in full"), msg("b", 1001.0)]),
        handoff_key("u1:C1", [msg("a")]),
    ]
    assert base not in variants and len(set(variants)) == len(variants)
