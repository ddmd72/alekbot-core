"""The owner-text scan after the cross-cut review (R2): preference facts, the workspace's scratch/ and files/, and the
owner-only scope are stated."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from scripts.validation import drive_intent_text_scan as script
from scripts.validation.drive_intent_text_scan_logic import STORES, scan
from src.domain.entities import FactDomain

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def _facts():
    by_domain = {FactDomain.AGENT_DIRECTIVE.value: [SimpleNamespace(id="d1", text="Be brief")],
                 FactDomain.PREFERENCE.value: [SimpleNamespace(id="p1", text="When filing scans call save_file_to_drive")]}
    return SimpleNamespace(get_active_facts_ordered=AsyncMock(side_effect=lambda account, domain: by_domain[domain]))


async def test_preference_facts_are_scanned_beside_the_directives():
    items, _, skipped = await script.collect(script.ScanSources(facts=_facts()), "u1", "a1", NOW)
    assert [(i.store, i.key) for i in items if i.store in ("directive", "preference")] == [
        ("directive", "d1"), ("preference", "p1")]
    assert {(h.store, h.pattern) for h in scan(items)} == {("preference", "save_file_to_drive")}
    assert "preference" in STORES and "directive" not in skipped


async def test_a_missing_fact_reader_reports_both_stores_as_not_scanned():
    _, _, skipped = await script.collect(script.ScanSources(), "u1", "a1", NOW)
    assert {"directive", "preference"} <= set(skipped)


async def test_the_workspace_scan_reads_scratch_and_the_text_files_of_files():
    entries = {"projects/": [], "references/": [],
               "scratch/": [SimpleNamespace(path="scratch/tmp.md", generation=1)],
               "files/": [SimpleNamespace(path="files/notes.md", generation=2),
                          SimpleNamespace(path="files/scan.pdf", generation=3)]}
    workspace = SimpleNamespace(list=AsyncMock(side_effect=lambda user, folder: entries[folder]),
                                read=AsyncMock(return_value=SimpleNamespace(data=b"call move_file_in_drive")))
    items, _, _ = await script.collect(script.ScanSources(workspace=workspace), "u1", "a1", NOW)
    assert [(i.store, i.key) for i in items] == [("workspace", "scratch/tmp.md"), ("workspace", "files/notes.md")]


def test_the_scan_states_that_only_the_owner_is_scanned_and_skill_files_are_redrafted_in_chat():
    doc = script.__doc__
    assert "Only the owner" in doc and "redrafted in chat" in doc
