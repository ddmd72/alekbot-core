"""Name keys are versioned on their own (USER_DRIVE_INDEXING_RFC §12.3): a change to the keyer re-keys records
through the upgrade pass, not through a policy bump."""
from dataclasses import replace

from src.domain import drive_index_planner as planner
from src.domain.drive_index import CURRENT_VERSIONS, NAME_KEYS_VERSION
from src.domain.drive_index_planner import stale_versions

from .test_drive_index_planner import ch, observe


def test_the_version_map_carries_the_name_keys_version():
    assert CURRENT_VERSIONS["name_keys"] == NAME_KEYS_VERSION


def test_a_record_stamps_the_version_its_keys_were_made_under():
    rec = observe(ch("a", "Report.pdf")).upsert
    assert rec.name_keys_version == NAME_KEYS_VERSION and not stale_versions(rec)
    assert stale_versions(replace(rec, name_keys_version=NAME_KEYS_VERSION - 1))


def test_a_re_plan_under_a_new_keyer_rewrites_the_keys(monkeypatch):
    stored = observe(ch("a", "Report.pdf")).upsert
    monkeypatch.setattr(planner, "NAME_KEYS_VERSION", NAME_KEYS_VERSION + 1)
    monkeypatch.setattr(planner, "name_keys", lambda name: ("w:new",))
    assert stale_versions(stored)
    rec = observe(ch("a", "Report.pdf"), stored).upsert
    assert rec.name_tokens == ("w:new",) and rec.name_keys_version == NAME_KEYS_VERSION + 1
    assert not stale_versions(rec)
