"""The specialist searches a subject once, in the user's words (rules of the former drive-search skill).

Pins the sentences restored by fix(drive) after the G3 over-search analysis: the set-level rule of
DRIVE_SPECIALIST_RFC §2.3.11 reaches the specialist's tokens and Smart's query-language line.
"""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOKENS = ROOT / "prompts_snapshot" / "tokens" / "system"


def _token(name: str) -> str:
    return (TOKENS / f"{name}.groovy").read_text(encoding="utf-8")


def test_the_whole_commission_searches_a_subject_once():
    text = _token("PROTOCOL_DRIVE_SEARCH")
    assert "This holds for the whole commission, not only for one query: search a subject once, with the user's own words." in text
    assert "the orchestrator commissions again with other words when it wants them." in text
    assert text.index("not the user's own name.") < text.index("This holds for the whole commission")


def test_d29_rule_moves_verbatim_from_the_skill():
    text = _token("PROTOCOL_DRIVE_SEARCH")
    assert "then answer from what you have." in text   # RFC §7.6: skill sentences move word for word

def test_parallel_searches_are_for_different_subjects():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert "in one turn: searches for different subjects, several opens, a batch of moves." in text
    assert "in one turn: several searches" not in text


def test_a_list_of_alternative_words_is_not_a_list_of_searches():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert (
        "A list of alternative words for one subject (synonyms, translations, other document types) is not a list "
        "of searches: search the user's own words once."
    ) in text


def test_the_commissions_quoted_words_are_the_users_in_one_language():
    text = _token("COGNITIVE_PROCESS_DRIVE")
    assert "for what to find; they are the user's words, in one language;" in text


def test_the_specialist_tokens_do_not_name_the_orchestrator_side():
    for name in ("PROTOCOL_DRIVE_SEARCH", "COGNITIVE_PROCESS_DRIVE"):
        text = _token(name)
        for banned in ("work_with_drive_files", "use_skill", "drive-search"):
            assert banned not in text, (name, banned)


SMART_LINE = (
    "quote the user's own few key words, in ONE language, as the user wrote them — "
    "no synonym lists, no translations, no extra words."
)


def test_smart_query_language_line_carries_the_discipline():
    assert SMART_LINE in _token("PROTOCOL_AGENT_SELECTION")


def test_the_uploads_copy_of_the_smart_line_is_in_sync():
    import json

    data = json.loads((ROOT / "firestore_utils" / "uploads" / "PROTOCOL_AGENT_SELECTION.json").read_text(encoding="utf-8"))
    assert SMART_LINE in data["content"]


def test_the_manifest_find_sentence_uses_the_skills_words():
    import src.infrastructure.agent_manifest as manifest

    source = Path(manifest.__file__).read_text(encoding="utf-8")
    assert "A find ('find', 'show me', 'do I have', 'where is', 'which files', 'everything about X') is" in source
    assert "(one language, no synonyms or translations)" in source


@pytest.mark.parametrize("name", ["PROTOCOL_DRIVE_SEARCH", "COGNITIVE_PROCESS_DRIVE"])
def test_the_specialist_tokens_exist(name):
    assert _token(name)
