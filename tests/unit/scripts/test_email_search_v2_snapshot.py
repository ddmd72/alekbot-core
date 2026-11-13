"""The email search v2 snapshot harness (search slice 4, Part B): `select` expands the owner's right ids to their
template series, `run` compares v2 with v1 per labelled query, `--prev` names what changed, and nothing is written
outside scripts/memory/. Invented cast only; no network (an in-test embedding service)."""
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from scripts.validation.email_eval import search_v2_snapshot as snap
from src.domain.email_search_render import MailSearchResult
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from src.ports.embedding_service import EmbeddingService
from tests.unit.domain.email_search_fixtures import everything


def _record(email_id: str, address: str, subject: str) -> Dict[str, Any]:
    return {"email_id": email_id, "from_address": address, "subject": subject}


SHOP = "Supermercado Exemplo Lda. <info@supermercado.example>"
RECORDS = [
    _record("r1", SHOP, "Recibo de compra 14/01/2025"), _record("r2", SHOP, "Recibo de compra 11/02/2025"),
    _record("r3", SHOP, "Recibo de compra 22/09/2026"),
    _record("f1", "Ferretería El Tornillo Feliz <info@tornillo.example>", "Factura FA-0137"),
    _record("b1", "info@albergo.example", "Prenotazione confermata RM-55120"),
    _record("s1", "Example School <info@school.example>", "Example School news, February"),
    _record("s2", "Example School <info@school.example>", "Example School news, April"),
    _record("m1", "Example Mutual Insurance <info@mutual.example>", "Apólice MP-2026-118204"),
]


def _label(item_id: str, request: str, **fields: Any) -> Dict[str, Any]:
    base = {"item_id": item_id, "trace_id": f"tr_{item_id}", "request": request, "returned_ids": [], "right_ids": [],
            "not_right_ids": [], "free_text": "", "none": False, "skip": False, "answered": True}
    base.update(fields)
    return base


def _result(mode: str = "ranked", shown=(), folded=None, ranked=()) -> MailSearchResult:
    return MailSearchResult("", mode, tuple(shown), folded or {}, tuple(ranked), len(ranked))


# -- select ---------------------------------------------------------------------------------------------------------

def test_select_expands_a_right_id_to_its_whole_template_series():
    labels = [_label("a", "x", right_ids=["r1"])]
    assert snap.select_ids(labels, RECORDS, 0, 1) == ["r1", "r2", "r3"]


def test_select_takes_right_ids_of_answered_items_only_and_each_id_once():
    labels = [_label("a", "x", right_ids=["f1", "r2"]), _label("b", "x", right_ids=["m1"], skip=True),
              _label("c", "x", right_ids=["b1"], answered=False), _label("d", "x", right_ids=["f1"])]
    assert snap.select_ids(labels, RECORDS, 0, 1) == ["f1", "r2", "r1", "r3"]


def test_select_adds_seeded_random_others_never_a_selected_id():
    labels = [_label("a", "x", right_ids=["f1"])]
    first = snap.select_ids(labels, RECORDS, 3, 7)
    assert first == snap.select_ids(labels, RECORDS, 3, 7) and first[0] == "f1" and len(first) == 4
    assert len(set(first)) == 4 and snap.select_ids(labels, RECORDS, 3, 8) != first
    assert len(snap.select_ids(labels, RECORDS, 100, 7)) == len(RECORDS)


def test_a_record_without_a_sender_address_has_the_empty_domain():
    assert snap.record_template({"subject": "Hello 5"}) == "|hello #"


# -- Today ----------------------------------------------------------------------------------------------------------

def test_today_is_the_date_of_the_earliest_call_of_the_trace_in_the_zone():
    calls = [{"trace_id": "t1", "timestamp": "2026-11-13 04:00:00"}, {"trace_id": "t1",
                                                                      "timestamp": "2026-11-12 23:30:00"},
             {"trace_id": "t2", "timestamp": "2026-10-01 10:00:00"}]
    assert snap.today_of(calls, "t1", "Europe/Helsinki") == date(2026, 11, 13)      # 23:30 UTC is already the 13th
    assert snap.today_of(calls, "t1", "America/New_York") == date(2026, 11, 12)
    assert snap.today_of(calls, "t3", "Europe/Helsinki") is None
    assert snap.today_of([{"trace_id": "t1", "timestamp": "2026-11-12T23:30:00+00:00"}], "t1",
                         "Europe/Helsinki") == date(2026, 11, 13)


# -- verdicts -------------------------------------------------------------------------------------------------------

def _verdict(item: Dict[str, Any], result: MailSearchResult) -> str:
    return snap.item_metrics(item, result)["verdict"]


def test_more_right_mails_shown_than_v1_is_better_and_fewer_is_worse():
    item = _label("a", "x", right_ids=["a", "b"], returned_ids=["a", "z"], not_right_ids=["z"])
    assert _verdict(item, _result(shown=["a", "b"], ranked=["a", "b"])) == "better"
    assert _verdict(item, _result(shown=["q"], ranked=["q"])) == "worse"


def test_a_folded_right_mail_counts_as_shown():
    item = _label("a", "x", right_ids=["a", "b"], returned_ids=["a"])
    metrics = snap.item_metrics(item, _result(shown=["a"], folded={"a": ["b"]}, ranked=["a", "b"]))
    assert (metrics["right_shown"], metrics["right_folded"], metrics["verdict"]) == (1, 1, "better")


def test_equal_rights_are_decided_by_fewer_wrong_shown_then_same():
    item = _label("a", "x", right_ids=["a"], returned_ids=["a", "z"], not_right_ids=["z"])
    assert _verdict(item, _result(shown=["a"], ranked=["a"])) == "better"
    assert _verdict(item, _result(shown=["a", "z"], ranked=["a", "z"])) == "same"
    assert _verdict(_label("a", "x", right_ids=["a"], returned_ids=["a"], not_right_ids=["z"]),
                    _result(shown=["a", "z"], ranked=["a", "z"])) == "worse"


def test_metrics_say_where_the_right_mail_ranked():
    item = _label("a", "x", right_ids=["c"], returned_ids=[])
    metrics = snap.item_metrics(item, _result(shown=["a"], ranked=["a", "b", "c"]))
    assert (metrics["right_shown"], metrics["right_ranked"], metrics["first_right_rank"]) == (0, 1, 3)
    assert snap.item_metrics(item, _result())["first_right_rank"] is None


def test_an_item_with_free_text_and_no_right_id_needs_an_id():
    assert _verdict(_label("a", "x", free_text="the one about the lock"), _result()) == "needs_id"
    assert _verdict(_label("a", "x", free_text="note", right_ids=["a"]), _result(shown=["a"], ranked=["a"])) != "needs_id"


def test_a_none_item_is_better_when_v2_says_nothing_and_v1_returned_mail_and_worse_when_v2_shows_lines():
    none = _label("a", "x", none=True, returned_ids=["z"])
    assert _verdict(none, _result("nothing")) == "better"
    assert _verdict(none, _result(shown=["q"], ranked=["q"])) == "worse"
    assert _verdict(_label("a", "x", none=True), _result("nothing")) == "same"
    assert _verdict(_label("a", "x", none=True), _result(shown=["q"], ranked=["q"])) == "worse"


# -- the diff against a previous report -------------------------------------------------------------------------------

def test_prev_names_the_items_whose_shown_ids_or_verdict_changed():
    prev = {"items": [{"item_id": "a", "shown_ids": ["x"], "verdict": "same"},
                      {"item_id": "b", "shown_ids": ["y"], "verdict": "same"},
                      {"item_id": "c", "shown_ids": [], "verdict": "worse"}]}
    now = {"items": [{"item_id": "a", "shown_ids": ["x"], "verdict": "same"},
                     {"item_id": "b", "shown_ids": ["y", "z"], "verdict": "same"},
                     {"item_id": "c", "shown_ids": [], "verdict": "better"},
                     {"item_id": "d", "shown_ids": [], "verdict": "same"}]}
    assert snap.changed_since(now, prev) == ["b", "c", "d"]


# -- the output path ------------------------------------------------------------------------------------------------

def test_an_output_path_outside_scripts_memory_is_refused(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    assert snap.checked_out_path(root / "report.json", root) == (root / "report.json").resolve()
    for bad in (tmp_path / "report.json", root / ".." / "report.json", root, Path("/tmp/x.json")):
        with pytest.raises(SystemExit):
            snap.checked_out_path(bad, root)


def test_the_commands_refuse_an_output_outside_scripts_memory(tmp_path):
    labels, export = tmp_path / "labels.json", tmp_path / "export.json"
    labels.write_text("[]")
    export.write_text("[]")
    with pytest.raises(SystemExit):
        snap.main(["select", "--labels", str(labels), "--export", str(export), "--out", str(tmp_path / "ids.txt")])
    with pytest.raises(SystemExit):
        snap.main(["run", "--cards", "c", "--labels", "l", "--calls", "k", "--zone", "Europe/Helsinki", "--out",
                   str(tmp_path / "r.json")])
    assert not (tmp_path / "ids.txt").exists()


def test_select_writes_one_id_per_line_under_the_memory_root(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "MEMORY_ROOT", tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    (tmp_path / "labels.json").write_text(json.dumps([_label("a", "x", right_ids=["f1"])]))
    (tmp_path / "export.json").write_text(json.dumps(RECORDS))
    out = tmp_path / "memory" / "ids.txt"
    assert snap.main(["select", "--labels", str(tmp_path / "labels.json"), "--export", str(tmp_path / "export.json"),
                      "--sample", "2", "--seed", "3", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "f1" and len(lines) == 3


# -- the cards file and the run ---------------------------------------------------------------------------------------

class _Embeddings(EmbeddingService):
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls.append(text)
        return [1.0, 0.0]

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [[1.0, 0.0] for _ in texts]


class _NoPeople(ConfirmedPeopleRepository):
    async def get(self, account_id: str) -> Optional[Any]:
        return None

    async def replace(self, account_id: str, people: Any, source: str) -> None:
        raise AssertionError("never written")


def _cards_file(tmp_path: Path) -> Path:
    rows = [{"email_id": c.mail.email_id, "document": {"id": c.mail.email_id}, "vector": [1.0, 0.0]}
            for c in everything()]
    rows.append({"email_id": "msg_new1", "document": None, "vector": None})
    path = tmp_path / "cards.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_the_cards_file_gives_cards_with_vectors_and_the_ids_with_no_card_yet(tmp_path):
    by_id = {c.mail.email_id: c for c in everything()}
    cards, uncarded = snap.load_cards(_cards_file(tmp_path), lambda doc: by_id[doc["id"]])
    assert len(cards) == len(by_id) and uncarded == ["msg_new1"]
    assert cards[0][1] == [1.0, 0.0]


LABELS = [
    _label("inv", "invoice FA-0137", right_ids=["msg_invoice01"], returned_ids=["msg_receipt_2026-09-08"],
           not_right_ids=["msg_receipt_2026-09-08"]),
    _label("air", "Example Air tickets", none=True, returned_ids=["msg_rome01"]),
    _label("lock", "the lock paper", free_text="it was the hardware shop"),
    _label("lost", "mail from September 2026", right_ids=["msg_invoice01"], trace_id="tr_missing"),
    _label("skipped", "invoice FA-0137", skip=True), _label("open", "invoice FA-0137", answered=False),
]
CALLS = [{"trace_id": "tr_inv", "timestamp": "2026-11-12 23:30:00"}, {"trace_id": "tr_air",
                                                                       "timestamp": "2026-11-12 23:31:00"},
         {"trace_id": "tr_lock", "timestamp": "2026-11-13 08:00:00"}]


async def _run(**overrides: Any) -> Dict[str, Any]:
    cards = [(c, [1.0, 0.0]) for c in everything()]
    args: Dict[str, Any] = dict(cards=cards, uncarded=["msg_new1"], labels=LABELS, calls=CALLS,
                                zone="Europe/Helsinki", slots={}, user_id="usr-1", account_id="acc-1",
                                embeddings=_Embeddings(), people=_NoPeople(), max_usd=0.20)
    args.update(overrides)
    return await snap.run_report(**args)


async def test_a_run_judges_each_answered_item_against_v1():
    report = await _run()
    by_id = {i["item_id"]: i for i in report["items"]}
    assert set(by_id) == {"inv", "air", "lock", "lost"}
    inv = by_id["inv"]
    assert inv["today"] == "2026-11-13" and inv["shown_ids"] == ["msg_invoice01"] and inv["mode"] == "ranked"
    assert (inv["right_shown"], inv["v1_right_shown"], inv["wrong_shown"], inv["verdict"]) == (1, 0, 0, "better")
    assert by_id["air"]["verdict"] == "better" and by_id["air"]["mode"] == "nothing"
    assert by_id["lock"]["verdict"] == "needs_id" and by_id["lost"]["verdict"] == "no_today"
    assert report["verdicts"] == {"better": 2, "same": 0, "worse": 0, "needs_id": 1, "no_today": 1}
    assert report["modes"] == {"nothing": 2, "ranked": 1} and report["stopped_at"] is None


async def test_a_run_reads_the_slots_of_an_item_and_keeps_the_top_fifty():
    report = await _run(slots={"inv": {"exact_keys": ["FA-0137"]}})
    inv = next(i for i in report["items"] if i["item_id"] == "inv")
    assert inv["shown_ids"] == ["msg_invoice01"] and len(inv["ranked_ids"]) <= 50


async def test_a_list_request_costs_nothing_and_the_run_stops_before_passing_the_budget():
    embeddings = _Embeddings()
    labels = [_label("sep", "mail from September 2026", right_ids=["msg_invoice01"], trace_id="tr_inv")]
    report = await _run(labels=labels, embeddings=embeddings)
    assert report["items"][0]["mode"] == "list" and embeddings.calls == []
    stopped = await _run(max_usd=0.0)
    assert stopped["stopped_at"] == "inv" and stopped["items"] == []
