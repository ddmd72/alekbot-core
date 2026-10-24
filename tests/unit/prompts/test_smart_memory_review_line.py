"""Smart's memory-review protocol (Facts S4 §3.10 last bullet, S4-D15, T7): `PROTOCOL_MEMORY_REVIEW` on the `smart` profile.

The pair in history is Alek's question; the owner answers by replying to the question message or in the Cabinet, and the
system takes that reply; Smart answers briefly when asked about it, never asks again, never raises a not-delivered question,
never calls `propose_memory_facts` for it and never says it saved anything. The frames it quotes are the domain builders'."""
import re

from src.domain.fact_review_frame import not_delivered_frame, reply_part, review_frame
from tests.support import prompt_vocabulary as pv
from tests.support.prompt_render import render_static, rendered_token_ids

TOKEN_ID = "PROTOCOL_MEMORY_REVIEW"
PROFILE = pv.load_profile("smart")
DOC = pv.ser.token_from_file(pv.token_path(TOKEN_ID).read_text(encoding="utf-8"))
BODY = DOC["content"]
PLACEHOLDER = "QID0"


def _shape(builder, *rest):
    return builder(PLACEHOLDER, *rest).replace(PLACEHOLDER, "<id>")


def _value(key):
    line = next(line for line in BODY.split("\n") if line.strip().startswith(f"{key}:"))
    return line.split(":", 1)[1].strip().strip('"')


def test_the_smart_profile_carries_the_token_at_order_270_non_overridable():
    assert PROFILE["tokens"][TOKEN_ID] == {"non_overridable": True, "order": 270}
    others = [int(t["order"]) for tid, t in PROFILE["tokens"].items() if tid != TOKEN_ID]
    assert 270 not in others


def test_the_token_renders_in_smarts_protocols_section_after_long_turns():
    assert DOC["class"] == "protocols" and DOC["category"] == "protocol"
    assert TOKEN_ID in rendered_token_ids("smart")
    text = render_static("smart")
    protocols = text[text.index("    protocols {"):]
    assert protocols.index("PROTOCOL_LONG_TURNS {") < protocols.index("PROTOCOL_MEMORY_REVIEW {")


def test_the_owner_answers_by_a_reply_to_the_question_message_or_in_the_cabinet():
    answers = _value("how_the_owner_answers")
    assert "replying to that question message (a Slack thread reply, a Telegram reply) or in the Cabinet" in answers
    assert "you see it only in history" in answers and "You are never asked to act on it." in answers
    assert "not an answer, even a bare digit" in _value("plain_messages")


def test_smart_answers_briefly_and_does_not_ask_again():
    assert "answer naturally and briefly" in _value("talking_about_it")
    assert "do not ask it again" in _value("question_pair")


def test_smart_never_says_it_saved_anything_about_a_memory_review_question():
    """Scoped to the question and its answer (T7 review S7), so a real receipt ("saved to your drive") stays sayable."""
    sentence = "About a memory-review question or its answer, never say you saved or remembered anything."
    assert sentence in _value("memory")
    saved = [m.start() for m in re.finditer(r"\bsaved?\b", BODY, re.IGNORECASE)]
    assert len(saved) == 1, "the only 'saved' in the token is the scoped prohibition"
    assert not re.search(r"(?<!, )never say you saved", BODY, re.IGNORECASE), "an unscoped prohibition"


def test_no_propose_memory_facts_for_a_memory_review_question_or_its_answer():
    assert "Do not call propose_memory_facts for a memory-review question or its answer" in _value("memory")
    assert BODY.count("propose_memory_facts") == 1


def test_a_not_delivered_question_is_not_raised():
    not_delivered = _value("not_delivered")
    assert _shape(not_delivered_frame) in not_delivered
    assert "the owner never saw it. Do not raise those questions." in not_delivered


def test_the_frames_the_token_quotes_are_the_builders():
    assert _shape(review_frame) in _value("question_pair")
    assert _shape(reply_part, "")[:-1] in _value("how_the_owner_answers")
    for match in re.finditer(r"\[(?:System|Reply to) memory review q=[^\]]*\]", BODY):
        assert match.group(0) in (_shape(review_frame), _shape(not_delivered_frame), _shape(reply_part, "")[:-1])


def test_the_token_round_trips_through_the_serializer():
    text = pv.token_path(TOKEN_ID).read_text(encoding="utf-8")
    assert pv.ser.token_to_file(pv.ser.token_from_file(text)) == text
    yaml_text = (pv.SNAPSHOT / "profiles" / "smart.yaml").read_text(encoding="utf-8")
    assert pv.ser.doc_to_yaml(pv.ser.doc_from_yaml(yaml_text)) == yaml_text
