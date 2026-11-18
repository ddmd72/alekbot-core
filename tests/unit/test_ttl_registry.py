"""P22 TTL registry: every writer of `expires_at` / `purge_after` under src/ is in config/ttl_registry.json, both ways,
the facts rows say "janitor, no TTL", and the runbook block of the deployment README is the one generated from the registry."""
from pathlib import Path

import pytest

from scripts import generate_ttl_runbook as gen
from tests.support import ttl_registry as reg

ENTRIES = reg.load()
ROOT = reg.REPO_ROOT
FORBIDDEN_POLICY = ("domain_facts", "fact_lifecycle_", "memory_write_leases", "fact_review_state")


def test_every_writer_is_registered_and_every_registered_writer_exists():
    found, listed = reg.scan_tree(), reg.registered(ENTRIES)
    assert sorted(found - listed) == [], "a writer of the field is not in config/ttl_registry.json"
    assert sorted(listed - found) == [], "a registry writer no longer writes the field (or the file is gone)"


def test_every_entry_has_a_known_deleter_and_what_a_reader_needs():
    ids = [e["id"] for e in ENTRIES]
    assert len(ids) == len(set(ids))
    for e in ENTRIES:
        assert e["field"] in reg.FIELDS and e["deleted_by"] in reg.DELETED_BY, e["id"]
        assert e["writers"] and all((ROOT / w).is_file() for w in e["writers"]), e["id"]
        if e["deleted_by"] == "ttl-policy":
            assert e["collection"] and e.get("what_expires"), e["id"]
        else:
            assert len(e.get("note", "").split()) >= 3, f"{e['id']}: a row without a policy says why"
        if e["deleted_by"] in ("provider", "not-stored"):
            assert e["collection"] is None, e["id"]


def test_the_facts_rows_say_janitor_and_no_ttl():
    facts = [e for e in ENTRIES if (e["collection"] or "").startswith("domain_facts")]
    assert [e["deleted_by"] for e in facts] == ["janitor"]
    assert "no TTL" in facts[0]["note"]


def test_no_ttl_policy_row_is_on_a_collection_whose_field_is_a_decision():
    bad = [e["id"] for e in ENTRIES if e["deleted_by"] == "ttl-policy" and any(m in (e["collection"] or "") for m in FORBIDDEN_POLICY)]
    assert bad == []


def test_a_collection_field_pair_has_one_row():
    pairs = [(e["collection"], e["field"]) for e in ENTRIES if e["collection"]]
    assert len(pairs) == len(set(pairs))


def test_the_committed_runbook_block_is_the_generated_one():
    text = (ROOT / "docs" / "07_deployment" / "README.md").read_text(encoding="utf-8")
    assert gen.apply(text, gen.render_block(ENTRIES)) == text, "run python -m scripts.generate_ttl_runbook"


def test_the_generated_block_holds_exactly_the_policy_and_unverified_rows():
    block = gen.render_block(ENTRIES)
    listed = {e["collection"] for e in ENTRIES if e["collection"] and e["deleted_by"] in ("ttl-policy", "unverified")}
    every = {e["collection"] for e in ENTRIES if e["collection"]}
    for collection in every:
        assert (f"| `<prefix>{collection}` |" in block) == (collection in listed), collection
    assert listed


# --- the scanner, on invented sources ----------------------------------------------------------------------------------

@pytest.mark.parametrize("source,fields", [
    ('d = {"expires_at": 1}', {"expires_at"}),
    ('C = "purge_after"\nd = {C: 1}', {"purge_after"}),
    ('x["expires_at"] = 1', {"expires_at"}),
    ('Model(expires_at=1, other=2)', {"expires_at"}),
    ('class M:\n    expires_at: int = 0', {"expires_at"}),
    ('obj.purge_after = 3', {"purge_after"}),
    ('v = data.get("expires_at")\nif v < now: pass', set()),
    ('doc = {"other": data["expires_at"]}', set()),
])
def test_the_scanner_tells_a_writer_from_a_reader(source, fields):
    assert reg.writers_in_source(source) == fields


def test_the_generator_check_names_drift(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(f"head\n{gen.BEGIN}\nstale\n{gen.END}\ntail\n")
    monkeypatch.setattr(gen, "README", readme)
    assert gen.main(["--check"]) == 1
    assert gen.main([]) == 0
    assert gen.main(["--check"]) == 0
    assert readme.read_text().startswith("head\n") and readme.read_text().endswith("tail\n")
