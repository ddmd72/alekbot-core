"""The drive routing probe after the move (DRIVE_SPECIALIST_RFC §4.1, §7.4): Smart reaches the drive through one
intent, and the commission's query carries what the user pointed at — the [File: …] label, the named folder, the
ref from history ("open the second one"). A removed drive intent is a miss."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.validation import drive_routing_probe as probe  # noqa: E402


def _scenario(text):
    return next(s for s in probe.SCENARIOS if s["text"].startswith(text))


def test_every_scenario_expects_the_one_drive_intent_or_forbids_it():
    assert {s["expect"] for s in probe.SCENARIOS} == {probe.DRIVE, None}
    assert _scenario("Запам'ятай, що")["forbid"] == probe.DRIVE


def test_the_second_one_needs_its_ref_in_the_query_and_not_the_firsts():
    sc = _scenario("Відкрий другий")
    assert probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "Open drive:BBB (lease_2025.pdf)"}])
    assert not probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "Open drive:AAA and drive:BBB"}])
    assert not probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "Open the second lease"}])


def test_a_save_into_a_named_folder_names_the_file_and_the_folder():
    sc = _scenario("Запам'ятай цей файл у папку Зустрічі")
    assert probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "Keep [File: lease_2026.pdf] in folder Зустрічі"}])
    assert not probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "Keep lease_2026.pdf"}])


def test_a_removed_drive_intent_is_a_miss_even_beside_the_right_call():
    sc = _scenario("Запам'ятай цей файл [File")
    calls = [{"intent": probe.DRIVE, "query": "keep lease_2026.pdf"}, {"intent": "save_file_to_drive", "query": ""}]
    assert not probe.judge_run(sc, calls)
    assert probe.judge_run(sc, calls[:1])


def test_a_memory_statement_never_reaches_the_drive():
    sc = _scenario("Запам'ятай, що")
    assert probe.judge_run(sc, [])
    assert not probe.judge_run(sc, [{"intent": probe.DRIVE, "query": "save a note"}])
