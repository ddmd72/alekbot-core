"""The summarizer's prompt and its response schema are one contract: the OUTPUT_FORMAT token is rendered (the blueprint's
`class_order` names its class, else it is read and never rendered), its field names are the schema's, and the
COGNITIVE_PROCESS no longer says the output is plain text."""
import re
from pathlib import Path

from firestore_utils.snapshot_serializer import token_from_file
from src.agents.lelik_summarizer_agent import _RESPONSE_SCHEMA
from tests.support.prompt_render import render_static, rendered_token_ids

_TOKENS = Path(__file__).resolve().parents[3] / "prompts_snapshot" / "tokens" / "system"
_CYRILLIC = re.compile("[Ѐ-ӿ]")


def _doc(token_id: str) -> dict:
    return token_from_file((_TOKENS / f"{token_id}.groovy").read_text(encoding="utf-8"))


def test_the_output_format_token_is_rendered():
    assert "OUTPUT_FORMAT_LELIK_SUMMARIZER" in rendered_token_ids("lelik_summarizer")
    prompt = render_static("lelik_summarizer")
    assert "Code reads your output, so it is one JSON object and nothing else" in prompt
    assert "Both fields are always present" in prompt


def test_the_token_names_exactly_the_schema_fields():
    content = _doc("OUTPUT_FORMAT_LELIK_SUMMARIZER")["content"]
    quoted = set(re.findall(r'\\"(\w+)\\"\s*:', content))
    assert quoted == set(_RESPONSE_SCHEMA["properties"])


def test_the_new_token_has_no_cyrillic():
    assert _CYRILLIC.search(_TOKENS.joinpath("OUTPUT_FORMAT_LELIK_SUMMARIZER.groovy").read_text(encoding="utf-8")) is None


def test_the_cognitive_process_no_longer_forbids_json_output():
    text = _TOKENS.joinpath("COGNITIVE_PROCESS_LELIK_SUMMARIZER.groovy").read_text(encoding="utf-8")
    assert "no JSON" not in text
    assert "first word of your output" not in text
    assert "plain-text note" not in text
    # The opening rule binds the summary field: the note opens with a word, and the output format says summary IS
    # the note, both in the one rendered prompt (the whole output opens with "{").
    assert "It opens with a word, never an emoji." in text
    prompt = render_static("lelik_summarizer")
    assert "It opens with a word, never an emoji." in prompt
    assert "summary is the note itself, exactly as the rules above describe it" in prompt
