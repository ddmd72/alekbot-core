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


def stored_and_read_back(messages):
    from src.adapters.firestore_session_store import FirestoreSessionStore
    store = FirestoreSessionStore(None)
    return store._deserialize_history(store._serialize_history(messages))


def at(created_at, text="Is the car service still on 2026-11-05?", role="user"):
    return Message(role=role, parts=[MessagePart(text=text)], created_at=created_at)


def test_a_batch_read_back_from_the_store_gives_the_same_key_as_the_live_batch():
    live = [msg("Is the car service still on 2026-11-05?", 1793836800.123456),
            Message(role="model", parts=[MessagePart(text="", full_text="Yes, bring the logbook.",
                                                     consolidation_text="")], created_at=1793836801.5),
            Message(role="user", parts=[MessagePart(), MessagePart(text="Is the gym membership valid until November?")],
                    created_at=1793836860.0)]
    read_back = stored_and_read_back(live)
    assert read_back[1].parts[0].text is None                       # the store does not write an empty text
    assert handoff_key("u1:C1", read_back) == handoff_key("u1:C1", live)


def test_another_text_role_or_instant_still_gives_another_key():
    base = handoff_key("u1:C1", [at(1793836800.0)])
    variants = [handoff_key("u1:C1", [at(1793836800.000001)]),
                handoff_key("u1:C1", [at(1793836800.0, text="Yes, until Nov 2026.")]),
                handoff_key("u1:C1", [at(1793836800.0, text="")]),
                handoff_key("u1:C1", [at(1793836800.0, role="model")])]
    assert base not in variants and len(set(variants)) == len(variants)
