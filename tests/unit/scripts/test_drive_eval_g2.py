"""G2 in the drive eval (USER_DRIVE_INDEXING_RFC §17.8, §24.11): a case hits when its paths are in the top K and its
expected substring is in the window of the card holding each path; OCR and goal labels are scopes; the bars pin the
served bytes; the content ports reach the tick; several gold files make one run."""
import json
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval import runner
from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from scripts.validation.drive_eval.metrics import (
    OVERALL, CaseOutcome, bar_mismatch, expected_substrings, make_bars, normalize_snippet, recall, substring_hit,
)
from scripts.validation.drive_eval.runner import EvalError, run_cases, run_eval, validate_gold
from src.domain.drive_search import DriveSearchResult

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import GOLD, MANIFEST, fake_content, rome

LEASE = "Contracts/Lease agreement — Porto.pdf"
LEASE_COPY = "Inbox/Lease agreement — Porto.pdf"
G2 = {"id": "g2a", "case": "clause", "query": "notice period", "lang": "en", "goal": "G2", "filters": {},
      "expected_paths": [LEASE], "expected_substring": "sixty (60) days"}


def test_snippets_compare_after_nfkc_casefold_and_whitespace():
    assert normalize_snippet("Итого: 1 030,00\n руб") == "итого: 1 030,00 руб"
    assert normalize_snippet("Счёт­фактура") == "счётфактура"
    assert substring_hit({"a": "1 030,00"}, {"a": "ИТОГО: 1 030,00"})
    assert not substring_hit({"a": "1 030,00"}, {"b": "1 030,00"})


def test_a_plain_substring_belongs_to_the_first_path():
    assert expected_substrings(["a", "b"], "x") == {"a": "x"}
    assert expected_substrings(["a", "b"], {"b": "y"}) == {"b": "y"}
    assert expected_substrings(["a"], None) == {}


def test_labels_are_scopes():
    table = recall([CaseOutcome("1", "amount", True, ("G2", "ocr")), CaseOutcome("2", "exact", False, ("G1",))])
    assert table["ocr"] == {"cases": 1, "hits": 1, "recall": 1.0} and table["G1"]["recall"] == 0.0
    assert table[OVERALL]["cases"] == 2


def test_bars_pin_the_served_bytes():
    outcomes = [CaseOutcome("1", "exact", True)]
    bars = make_bars(recall(outcomes), outcomes, 8, "fake/8", "now", "t", "g", "f" * 64)
    assert bars["files_sha256"] == "f" * 64
    assert bar_mismatch(bars, 8, "t", "g", ["1"], "f" * 64) == []
    assert bar_mismatch(bars, 8, "t", "g", ["1"], "e" * 64) == [
        "the fixture files (or a content template) changed since the bars were recorded"]


@pytest.mark.parametrize("broken, message", [
    ({**G2, "expected_substring": ""}, "expected_substring is empty"),
    ({**G2, "expected_substring": 5}, "a string or a"),
    ({**G2, "expected_substring": {"Nowhere.pdf": "x"}}, "not expected paths"),
    ({k: v for k, v in G2.items() if k != "expected_substring"}, "a G2 case needs expected_substring"),
    ({**G2, "goal": "G9"}, "goal must be one of"),
    ({**G2, "ocr": "yes"}, "ocr must be true or false"),
])
def test_malformed_g2_cases_are_refused(broken, message):
    paths = FixtureDrive(MANIFEST).paths()
    with pytest.raises(EvalError, match=message):
        validate_gold([broken], paths)


def test_a_well_formed_g2_case_passes():
    validate_gold([G2, {**G2, "id": "g2b", "expected_substring": {LEASE: "x"}, "ocr": True}],
                  FixtureDrive(MANIFEST).paths())


def _card(path, window, also_at=()):
    return SimpleNamespace(ref=f"drive:{item_id_for(path)}", path=path, also_at=also_at, passage=window)


class _Search:
    def __init__(self, cards):
        self.cards = cards

    async def search(self, user_id, request):
        return DriveSearchResult("ok", cards=self.cards)


async def test_a_g2_case_needs_the_substring_in_its_cards_window():
    drive = FixtureDrive(MANIFEST)
    hit = await run_cases(_Search([_card(LEASE, "5. Notice. ... with sixty (60) days' written notice")]), drive, [G2])
    assert hit.cases[0]["hit"] and hit.cases[0]["path_hit"] and hit.cases[0]["substring_hit"]
    assert hit.outcomes[0].labels == ("G2",)
    miss = await run_cases(_Search([_card(LEASE, "3. Rent. EUR 1,250.00")]), drive, [G2])
    row = miss.cases[0]
    assert not row["hit"] and row["path_hit"] and not row["substring_hit"]
    assert row["windows"] == {LEASE: "3. Rent. EUR 1,250.00"}


async def test_a_collapsed_copy_shows_its_window_for_both_paths():
    drive = FixtureDrive(MANIFEST)
    case = {**G2, "expected_paths": [LEASE_COPY], "ocr": True}
    run = await run_cases(_Search([_card(LEASE, "sixty (60) days", also_at=(LEASE_COPY,))]), drive, [case])
    assert run.cases[0]["hit"] and run.outcomes[0].labels == ("G2", "ocr")


async def test_a_g1_case_ignores_windows():
    drive = FixtureDrive(MANIFEST)
    run = await run_cases(_Search([_card("Scans/Scan_0012.pdf", "")]), drive, [GOLD[0]])
    assert run.cases[0]["hit"] and "substring_hit" not in run.cases[0]


async def test_the_content_ports_reach_the_tick(monkeypatch):
    seen = {}

    class _Service:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(runner, "DriveIndexingService", _Service)
    extractors, triage = {"parser": object()}, object()
    with pytest.raises(AttributeError):   # the stub has no tick; only the constructor is under test
        await runner.index_fixture(FixtureDrive(MANIFEST), runner.InMemoryDriveIndexRepository(), FakeEmbeddings(),
                                   rome, "fake/8", runner.EvalClock(), extractors=extractors, triage=triage)
    assert seen["extractors"] is extractors and seen["triage"] is triage


async def test_no_content_ports_leave_the_constructor_as_slice_1_built_it(monkeypatch):
    seen = {}

    class _Service:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(runner, "DriveIndexingService", _Service)
    with pytest.raises(AttributeError):
        await runner.index_fixture(FixtureDrive(MANIFEST), runner.InMemoryDriveIndexRepository(), FakeEmbeddings(),
                                   rome, "fake/8", runner.EvalClock())
    assert "extractors" not in seen and "triage" not in seen


async def test_two_gold_files_make_one_run_and_one_gold_hash(tmp_path):
    tree, g1, g2 = tmp_path / "tree.json", tmp_path / "g1.json", tmp_path / "g2.json"
    tree.write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    g1.write_text(json.dumps(GOLD[:2], ensure_ascii=False), encoding="utf-8")
    g2.write_text(json.dumps(GOLD[2:], ensure_ascii=False), encoding="utf-8")
    report = await run_eval(tree, [g1, g2], tmp_path / "bars.json", tmp_path / "r.json", embeddings=FakeEmbeddings(),
                            embedding_space="fake/8", place_of=rome, record=True, throttle_pause_s=0,
                            **fake_content())
    bars = json.loads((tmp_path / "bars.json").read_text(encoding="utf-8"))
    assert len(report["cases"]) == len(GOLD) and set(bars["cases"]) == {c["id"] for c in GOLD}
    assert bars["gold_sha256"] == runner.gold_sha256([g1, g2]) != runner.sha256_of(g1)
    assert bars["files_sha256"] == FixtureDrive.from_file(tree).content_sha256()
    assert report["gold"] == [str(g1), str(g2)] and "downloads" in report["index"]
    g2.write_text(json.dumps(GOLD[2:3], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvalError, match="the gold files changed"):
        await run_eval(tree, [g1, g2], tmp_path / "bars.json", tmp_path / "r.json", embeddings=FakeEmbeddings(),
                       embedding_space="fake/8", place_of=rome, record=False, throttle_pause_s=0,
                       **fake_content())


def test_the_cli_reads_both_committed_gold_files_by_default():
    from scripts.validation.drive_eval.__main__ import _FIXTURES, _args
    assert _args([]).gold == [_FIXTURES / "gold_g1.json", _FIXTURES / "gold_g2.json"]
    assert _args(["--gold", "x.json"]).gold[0].name == "x.json" and len(_args(["--gold", "x.json"]).gold) == 1


def test_the_cli_gives_no_verdict_without_the_content_ports(tmp_path, monkeypatch, capsys):
    from scripts.validation.drive_eval import __main__ as cli
    from scripts.validation.drive_eval import content_wiring

    def unavailable():
        raise content_wiring.ContentSetupError("ANTHROPIC_API_KEY is not set (.env)")

    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("GEMINI_API_KEY", "offline")
    monkeypatch.setattr(content_wiring, "build_real_content", unavailable)
    assert cli.main(["--report", str(tmp_path / "r.json")]) == runner.EXIT_SETUP
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("provider, key", [("", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_API_KEY"),
                                           ("openai", "OPENAI_API_KEY")])
def test_content_wiring_names_the_missing_key(monkeypatch, provider, key):
    from scripts.validation.drive_eval import content_wiring
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", provider)
    monkeypatch.delenv(key, raising=False)
    with pytest.raises(content_wiring.ContentSetupError, match=key):
        content_wiring.build_real_content()


def test_content_wiring_refuses_an_unknown_provider(monkeypatch):
    from scripts.validation.drive_eval import content_wiring
    monkeypatch.setenv("DRIVE_EVAL_PROVIDER", "gemini")
    with pytest.raises(content_wiring.ContentSetupError, match="claude or openai"):
        content_wiring.build_real_content()


async def test_the_eval_user_runs_on_the_requested_provider():
    from scripts.validation.drive_eval.content_wiring import _EvalUsers
    user = await _EvalUsers("openai").get_user("eval-user")
    assert user.config.agent_providers == {"drive_reader": "openai", "drive_triage": "openai"}
    assert (await _EvalUsers("").get_user("eval-user")).config.agent_providers is None
