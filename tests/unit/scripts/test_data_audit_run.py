"""scripts/data_audit/run.py: reads the configured collection, filters, reports, writes only its report file."""
import ast
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from google.cloud.firestore_v1.vector import Vector

from scripts.data_audit import run as runner
from src.config.environment import EnvironmentConfig
from tests.support.firestore_eval import EvalDb

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECTION = "eval_domain_facts"
NOW = datetime(2026, 3, 4, 10, 0, 5, tzinfo=timezone.utc)
SECRET = "SECRET-FACT-TEXT"


def fact(doc_id, account="acc-1", lineage=None, **over):
    doc = {
        "id": doc_id, "account_id": account, "created_by_user_id": "usr-1", "lineage_id": lineage or f"L-{doc_id}",
        "text": SECRET, "type": "event", "state": "current", "is_current": True, "valid_to": None,
        "vector": Vector([0.1]), "tags_vector": Vector([0.2]), "metadata_vector": Vector([0.3]),
        "created_at": NOW, "reported_date": NOW, "valid_from": NOW, "last_updated": NOW,
    }
    doc.update(over)
    return doc


def seeded():
    db = EvalDb()
    docs = db.collection(COLLECTION).docs
    docs["a1"] = fact("a1")
    docs["a2"] = fact("a2", id="wrong")                        # id_mismatch
    docs["a3"] = fact("a3", lineage="L-shared")
    docs["a4"] = fact("a4", lineage="L-shared")               # two current in one lineage
    docs["b1"] = fact("b1", account="acc-2")                   # not read under --account acc-1
    del docs["b1"]["created_at"]                               # missing_created_at
    db.collection("eval_other").docs["x"] = {"id": "x"}        # another collection: never read
    return db


def report_of(path):
    return json.loads(path.read_text(encoding="utf-8"))


async def test_audits_the_whole_configured_collection_and_writes_the_report(tmp_path):
    path = await runner.run(seeded(), "facts", COLLECTION, tmp_path, now=NOW)
    assert path == tmp_path / "data_audit_facts_20260304T100005Z.json"
    report = report_of(path)
    assert report["documents"] == 5 and report["collection"] == COLLECTION and report["account_filtered"] is False
    assert report["generated_at"] == NOW.isoformat()
    assert report["classes"] == {
        "id_mismatch": {"count": 1, "doc_ids": ["a2"]},
        "lineage_multiple_current": {"count": 2, "doc_ids": ["a3", "a4"]},
        "missing_created_at": {"count": 1, "doc_ids": ["b1"]},
    }
    assert list(tmp_path.iterdir()) == [path]


async def test_account_filter_reads_only_that_account(tmp_path):
    report = report_of(await runner.run(seeded(), "facts", COLLECTION, tmp_path, account_id="acc-1", now=NOW))
    assert report["documents"] == 4 and report["account_filtered"] is True
    assert "missing_created_at" not in report["classes"]
    assert "acc-1" not in json.dumps(report)


async def test_limit_reads_at_most_n_documents(tmp_path):
    report = report_of(await runner.run(seeded(), "facts", COLLECTION, tmp_path, limit=2, now=NOW))
    assert report["documents"] == 2 and report["limit"] == 2


async def test_a_clean_collection_reports_no_class(tmp_path):
    db = EvalDb()
    db.collection(COLLECTION).docs["a1"] = fact("a1")
    report = report_of(await runner.run(db, "facts", COLLECTION, tmp_path, now=NOW))
    assert report["documents"] == 1 and report["classes"] == {}


async def test_stdout_has_counts_only(tmp_path, capsys):
    await runner.run(seeded(), "facts", COLLECTION, tmp_path, now=NOW)
    out = capsys.readouterr().out
    assert "facts: 5 documents" in out and "id_mismatch: 1" in out and "lineage_multiple_current: 2" in out
    for hidden in ("a2", "a3", "b1", SECRET, "acc-1"):
        assert hidden not in out.replace("data_audit_facts_20260304T100005Z", "")


async def test_the_report_holds_no_field_values(tmp_path):
    text = (await runner.run(seeded(), "facts", COLLECTION, tmp_path, now=NOW)).read_text(encoding="utf-8")
    assert SECRET not in text and "usr-1" not in text


async def test_the_run_writes_nothing_to_the_database(tmp_path):
    db = seeded()
    before = {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in db.cols.items()}
    await runner.run(db, "facts", COLLECTION, tmp_path, now=NOW)
    await runner.run(db, "facts", COLLECTION, tmp_path, account_id="acc-1", limit=3, now=NOW)
    assert {name: (col.docs, col.versions) for name, col in db.cols.items()} == before
    assert db.commits == []


def test_the_modules_contain_no_firestore_write_call():
    writes = {"set", "update", "delete", "batch", "transaction", "create", "add", "commit"}
    for name in ("run.py", "facts.py"):
        tree = ast.parse((REPO_ROOT / "scripts" / "data_audit" / name).read_text(encoding="utf-8"))
        called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not called & writes, (name, called & writes)


def test_the_report_directory_is_the_gitignored_scripts_memory():
    assert runner.MEMORY_DIR == REPO_ROOT / "scripts" / "memory"
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "scripts/memory/" in ignored


def test_each_entity_names_a_real_collection_and_its_checks():
    for entity, spec in runner.CHECKERS.items():
        assert isinstance(getattr(EnvironmentConfig, spec["collection"]), property), entity
        assert callable(spec["per_doc"]) and callable(spec["across"]) and spec["keep"]


def test_an_unknown_entity_is_refused_by_the_command_line():
    with pytest.raises(SystemExit):
        runner.main(["nothing"])
