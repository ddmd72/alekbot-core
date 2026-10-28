"""The specialist searches a subject once, in the user's words (rules of the former drive-search skill).

Reviewer (owner decision 2026-10-28): the specialist's tokens are rewritten while they are tuned on evals, so no test
pins their wording; what stays is structural (the tokens exist and name nothing of the orchestrator's side) and the
Smart-side query-language line and manifest sentence, which this rewrite does not touch.
"""
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOKENS = ROOT / "prompts_snapshot" / "tokens" / "system"


def _token(name: str) -> str:
    return (TOKENS / f"{name}.groovy").read_text(encoding="utf-8")


# Reviewer: test_the_whole_commission_searches_a_subject_once, test_d29_rule_moves_verbatim_from_the_skill,
# test_parallel_searches_are_for_different_subjects, test_a_list_of_alternative_words_is_not_a_list_of_searches and
# test_the_commissions_quoted_words_are_the_users_in_one_language retired: each pinned a sentence of the specialist's
# tokens word for word (owner decision 2026-10-28: no wording pins while the prompt is tuned on evals).


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
    # Reviewer (USER_DRIVE_INDEXING_RFC §28.3 A13): the find sentence keeps its file words without "everything about X"
    assert "A find ('find', 'show me', 'do I have', 'where is', 'which files') is" in source
    assert "(one language, no synonyms or translations)" in source


@pytest.mark.parametrize("name", ["PROTOCOL_DRIVE_SEARCH", "COGNITIVE_PROCESS_DRIVE"])
def test_the_specialist_tokens_exist(name):
    assert _token(name)
