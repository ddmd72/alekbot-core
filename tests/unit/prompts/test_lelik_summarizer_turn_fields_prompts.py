"""The file-mode summarizer prompts explain the turn fields `summarizer_turns` sends: the interpreter tells an ask turn
from a translation and an interrupted line from a whole one; the tutor reads every turn as an exchange and never
claims history the model does not have."""
import re
from pathlib import Path

from firestore_utils.snapshot_serializer import token_from_file
from src.domain.call_text_file import TURN_KIND_ASK, TURN_KIND_EXCHANGE
from tests.support.prompt_render import render_static

_TOKENS = Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"


def _content(token_id: str) -> str:
    return token_from_file((_TOKENS / f"{token_id}.groovy").read_text(encoding="utf-8"))["content"]


def test_the_interpreter_names_both_kinds_and_the_interrupted_flag():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_INTERPRETER")
    assert f"A turn whose kind is {TURN_KIND_EXCHANGE} is one translation" in text
    assert f"A turn whose kind is {TURN_KIND_ASK} is never a translation" in text
    assert "`request_text` is the owner's question to you, and `response_text` is your answer to the owner" in text
    assert "its `interrupted` is true when you were cut off before you finished" in text
    assert "An interrupted exchange was cut off before you finished" in text
    assert "never fills the gap with a guess" in text
    assert "Say who said something only when the turns make it clear" in text


def test_the_interpreter_takes_names_and_figures_from_what_was_said_in_the_owners_language():
    # The source is what was spoken and outranks the rendering; a name keeps its spelling, while a figure keeps its
    # value but is written in the owner's language (the note's language rule), never in how the other side worded it.
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_INTERPRETER")
    rule = re.search(r'"([^"]*from `request_text`, as it was spoken[^"]*)"', text).group(1)
    assert rule.startswith("Take every ")
    for detail in ("name", "figure", "date", "time", "money", "address", "document number"):
        assert detail in rule.split(":")[0]
    assert "the spoken one is authoritative" in rule
    spelling, value = rule[rule.index("authoritative."):].split(";")
    assert all(d in spelling for d in ("name", "address", "document number")) and "keeps its spelling" in spelling
    assert all(d in value for d in ("figure", "date", "time", "sum")) and "keeps its value" in value
    assert "written in the owner's language" in value


def test_the_tutor_reads_every_turn_as_an_exchange():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_TUTOR")
    assert f"every turn's kind is {TURN_KIND_EXCHANGE}" in text
    assert f"kind is {TURN_KIND_ASK}" not in text
    assert "translation" not in text
    assert "`interrupted` is true when you were cut off before you finished" in text
    assert "never completes it with a guess" in text


def test_the_tutor_claims_no_history_beyond_the_lesson():
    text = _content("COGNITIVE_PROCESS_LELIK_SUMMARIZER_TUTOR")
    assert "any form you corrected earlier in this lesson that the owner later got right" in text
    assert "used to get wrong" not in text


def test_the_changed_sentences_reach_the_rendered_prompts():
    assert f"A turn whose kind is {TURN_KIND_ASK} is never a translation" in render_static("lelik_summarizer_interpreter")
    assert "earlier in this lesson that the owner later got right" in render_static("lelik_summarizer_tutor")
