"""The answer-from-content S3 bar (plan §5 S3), offline: a content case's commission to the drive specialist names no
method (open, verbatim, full text, and their translations) unless it is a whole-file case; the new gold validates in
the G3 schema beside the existing golds; the language-bound answer groups of the S2 gold hold Ukrainian forms."""
import json
from pathlib import Path

import pytest

from scripts.validation.drive_eval import smart_dry_run as g3
from scripts.validation.drive_eval import specialist_ab as ab
from src.infrastructure.agent_manifest import Intent

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "drive_eval"
_GOLD = [json.loads((_FIX / n).read_text(encoding="utf-8")) for n in ("gold_g3.json", "gold_g3_v2.json", "gold_g3_afc.json")]
AFC = _GOLD[2]


def _row(query, **flags):
    return {"id": "c", "repeat": 0, "ops": [], "calls": [{"intent": Intent.WORK_WITH_DRIVE_FILES, "query": query}],
            "content": True, **flags}


def test_the_new_gold_validates_with_the_existing_ones_and_has_the_shape_the_plan_names():
    g3.validate_g3(_GOLD[0] + _GOLD[1] + AFC)
    assert len(AFC) >= 10 and all(c["content"] and c["set"] == "positive" for c in AFC)
    assert sum(1 for c in AFC if c.get("whole_file")) == 2
    assert any(c["id"].startswith("afc-g3-everything") for c in AFC) and any("-method-" in c["id"] for c in AFC)
    assert all(c["id"].startswith("afc-g3-") and c.get("isolate") for c in AFC)
    assert {"en", "uk"} <= {c["lang"] for c in AFC}


def test_whole_file_needs_content():
    bad = dict(AFC[0], id="x", content=None)
    bad.pop("content")
    bad["whole_file"] = True
    with pytest.raises(g3.G3Error, match="whole_file belongs to a content case"):
        g3.validate_g3(_GOLD[0] + AFC + [bad])


@pytest.mark.parametrize("query", [
    "Open Tom's plan and return its full content verbatim", "Find the review process and report the reviewer rules; open it",
    "Return the full text of the data retention specification", "Search R&D for the retention periods, word for word",
    "Відкрий план Тома і поверни дослівно", "Открой документ и верни полный текст", "Read the entire document"])
def test_a_content_commission_naming_a_method_is_a_hard_failure(query):
    failures = g3.hard_failures([_row(query)])
    assert len(failures) == 1 and "names a method" in failures[0]


@pytest.mark.parametrize("query", [
    "Find in the R&D folder how many reviewers a specification needs and how long they have to comment",
    "What is the 95th percentile latency limit in Tom's plan?", "Tell me everything the R&D folder says about retention",
    "Скільки днів зберігаються сирі повідомлення телеметрії?", "Какой срок хранения аудита?"])
def test_a_commission_without_a_method_passes(query):
    assert g3.hard_failures([_row(query)]) == []


def test_a_whole_file_case_may_name_the_method_and_a_non_content_case_is_not_checked():
    assert g3.hard_failures([_row("Open the review process and return its full text", whole_file=True)]) == []
    row = _row("Open the file")
    row["content"] = False
    assert g3.hard_failures([row]) == []


def test_only_commissions_to_the_drive_specialist_are_read():
    row = _row("hi")
    row["calls"] = [{"intent": Intent.SEARCH_MEMORY, "query": "open verbatim"}]
    assert g3.hard_failures([row]) == []


def test_the_s2_gold_accepts_a_correct_ukrainian_answer():
    gold = json.loads((_FIX / "gold_specialist_afc.json").read_text(encoding="utf-8"))
    case = next(c for c in gold if c["id"] == "afc-knowledge-reviewers")
    answer = "Два рецензенти, п'ять робочих днів на коментарі."
    assert ab.check_case(case, answer, [{"op": "search", "args": {}}], [{"intent": "work_with_drive_files"}]) == []
    wrong = "Три рецензенти, десять робочих днів."
    assert any("answer lacks" in f for f in ab.check_case(case, wrong, [{"op": "search", "args": {}}],
                                                          [{"intent": "work_with_drive_files"}]))


def test_the_ukrainian_forms_do_not_let_the_superseded_value_through():
    gold = json.loads((_FIX / "gold_specialist_afc.json").read_text(encoding="utf-8"))
    case = next(c for c in gold if c["id"] == "afc-value-raw-retention")
    ops, calls = [{"op": "search", "args": {}}], [{"intent": "work_with_drive_files"}]
    assert ab.check_case(case, "Сирі повідомлення зберігаються 400 днів.", ops, calls) == []
    assert any("holds" in f for f in ab.check_case(case, "Раніше 540 днів, тепер 400 днів.", ops, calls))
