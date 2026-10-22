"""The owner-text scan (DRIVE_SPECIALIST_RFC §10 step 6, §13 step 5): finds the drive's old intents, drive-only fields,
generic fields next to a drive intent and the drive-search skill in the owner's stored texts; the rollback mode finds
work_with_drive_files too. Read-only: the stores are fakes here, and every output lands under scripts/memory/."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts.validation import drive_intent_text_scan as script
from scripts.validation.drive_intent_text_scan_logic import (
    NEW_INTENT, OLD_INTENTS, ScanItem, backup_payload, render_report, scan, scan_item, skill_markdown)
from src.domain.entities import FactDomain
from src.domain.prompt_v3.slot import OwnerType
from src.infrastructure.agent_manifest import Intent
from src.utils.skill_md import parse_skill_md

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def test_the_old_intent_list_is_the_one_the_cut_removes():
    assert set(OLD_INTENTS) == {"search_files_in_drive", "save_file_to_drive", "list_files_in_drive",
                                "open_file_from_drive", "move_file_in_drive", "create_folder_in_drive",
                                "update_file_in_drive", "delete_file_from_drive"}
    assert NEW_INTENT == Intent.WORK_WITH_DRIVE_FILES


def test_an_intent_a_drive_field_and_the_skill_are_hits_wherever_they_appear():
    item = ScanItem("skill", "drive-inbox v3",
                    "Sort the Inbox.\n\nFirst load drive-search.\nThen call search_files_in_drive.\n\n\n\n"
                    "Keep drive_query short.")
    assert sorted((h.line_no, h.pattern) for h in scan_item(item)) == [
        (3, "drive-search"), (4, "search_files_in_drive"), (8, "drive_query")]


def test_a_generic_field_is_a_hit_only_near_a_drive_intent():
    near = ScanItem("reminder", "n1", "Use move_file_in_drive\nwith folder Archive\nand new_name report.pdf")
    far = ScanItem("reminder", "n2", "Put the text in the folder\n\n\n\n\nnothing about the drive here")
    assert {h.pattern for h in scan_item(near)} == {"move_file_in_drive", "folder", "new_name"}
    assert scan_item(far) == []


def test_names_inside_longer_words_are_not_hits():
    assert scan_item(ScanItem("directive", "d1", "the my_search_files_in_drive_helper and drive-search-v2 notes")) == []


def test_the_new_intent_is_found_only_in_the_rollback_scan():
    item = ScanItem("workspace", "projects/inbox.md", "Ask work_with_drive_files to list Inbox, folder Inbox")
    assert scan_item(item) == []
    assert {h.pattern for h in scan_item(item, include_new=True)} == {NEW_INTENT, "folder"}


def test_the_report_counts_by_store_and_the_backup_holds_only_items_that_hit():
    items = [ScanItem("skill", "drive-inbox v3", "call save_file_to_drive", 3), ScanItem("skill", "flight v1", "ok", 1)]
    hits = scan(items)
    report = render_report({"skill": 2}, hits, include_new=False)
    assert "| skill | 2 | 1 |" in report and "## skill: drive-inbox v3" in report and "save_file_to_drive" in report
    backup = backup_payload(items, hits)
    assert [i["key"] for i in backup["items"]] == ["drive-inbox v3"] and backup["items"][0]["version"] == 3
    assert "No hits" in render_report({"skill": 1}, [], include_new=False)


def test_an_exported_skill_reads_back_as_the_same_skill():
    body = "# Drive inbox\n\nSort `Inbox` by type: \"scans\", 'photos'."
    skill = parse_skill_md(skill_markdown("drive-inbox", "Use when sorting the drive's Inbox: names, folders.", body))
    assert (skill.name, skill.description, skill.body) == (
        "drive-inbox", "Use when sorting the drive's Inbox: names, folders.", body)


def _sources(**over):
    skill = SimpleNamespace(name="drive-inbox", description="Sort the Inbox.", body="Call list_files_in_drive.",
                            version=4, files=[SimpleNamespace(path="references/rules.md", sha256="h1")])
    skills = SimpleNamespace(list_current=AsyncMock(return_value=[skill]),
                             get_file=AsyncMock(return_value="Use drive_folder."))
    notes = SimpleNamespace(list_active_notes=AsyncMock(return_value=[SimpleNamespace(
        note_id="n1", text="Weekly sort", instruction="search_files_in_drive for new scans", workspace_file=None)]))
    facts = SimpleNamespace(get_active_facts_ordered=AsyncMock(return_value=[SimpleNamespace(id="f1", text="Be brief")]))
    entries = {"projects/": [SimpleNamespace(path="projects/inbox.md", generation=7),
                             SimpleNamespace(path="projects/scan.pdf", generation=8)], "references/": []}
    workspace = SimpleNamespace(list=AsyncMock(side_effect=lambda user, folder: entries[folder]),
                                read=AsyncMock(return_value=SimpleNamespace(data=b"open_file_from_drive the lease")))
    profiles = SimpleNamespace(get_override_tokens=AsyncMock(side_effect=lambda owner, oid: (
        {"VOICE_X": object()} if owner is OwnerType.USER else {"GONE": object()})))

    async def get_token(token_id):
        if token_id == "GONE":
            raise KeyError(token_id)
        return SimpleNamespace(content="no drive words")
    tokens = SimpleNamespace(get=get_token)
    fields = dict(skills=skills, notes=notes, facts=facts, workspace=workspace, profiles=profiles, tokens=tokens)
    fields.update(over)
    return script.ScanSources(**fields)


async def test_collect_reads_every_store_read_only():
    src = _sources()
    items, exports, skipped = await script.collect(src, "u1", "a1", NOW)
    assert [(i.store, i.key) for i in items] == [
        ("skill", "drive-inbox v4"), ("skill_file", "drive-inbox v4:references/rules.md"), ("reminder", "n1"),
        ("directive", "f1"), ("workspace", "projects/inbox.md"), ("prompt_override", "user:VOICE_X")]
    assert skipped == ["prompt_override account:GONE (token missing)"]
    src.facts.get_active_facts_ordered.assert_awaited_once_with("a1", domain=FactDomain.AGENT_DIRECTIVE.value)
    src.notes.list_active_notes.assert_awaited_once_with("u1", NOW)
    assert set(exports) == {"drive-inbox.v4", "drive-inbox.v4.files/references/rules.md"}
    assert {h.pattern for h in scan(items)} == {"list_files_in_drive", "drive_folder", "search_files_in_drive",
                                                "open_file_from_drive"}


async def test_a_store_without_a_reader_is_reported_not_scanned(tmp_path, monkeypatch):
    items, exports, skipped = await script.collect(_sources(workspace=None, skills=None), "u1", "a1", NOW)
    assert {"workspace", "skill", "skill_file"} <= set(skipped)
    monkeypatch.setattr(script, "_MEMORY", tmp_path)
    report, n = script.write_outputs(tmp_path / "scan", items, exports, skipped, include_new=False)
    assert "Not scanned (no reader configured): skill, skill_file, workspace" in report.read_text()


async def test_outputs_land_in_one_folder_with_report_backup_and_skill_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "_MEMORY", tmp_path)
    items, exports, skipped = await script.collect(_sources(), "u1", "a1", NOW)
    out = script.resolve_out(tmp_path / "scan", "stamp")
    report, n = script.write_outputs(out, items, exports, skipped, include_new=False)
    assert n == 4 and report.exists()
    assert json.loads((out / "backup.json").read_text())["items"]
    assert parse_skill_md((out / "skills" / "drive-inbox.v4.md").read_text()).body == "Call list_files_in_drive."
    assert (out / "skills" / "drive-inbox.v4.files" / "references" / "rules.md").read_text() == "Use drive_folder."


def test_the_output_folder_must_be_under_scripts_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(script, "_MEMORY", tmp_path / "memory")
    with pytest.raises(SystemExit, match="scripts/memory"):
        script.resolve_out(tmp_path / "elsewhere", "stamp")


def test_a_rewritten_skill_must_parse_and_name_no_old_intent(tmp_path):
    good = skill_markdown("drive-inbox", "Use when sorting the drive's Inbox.", "Find the new files on the drive, then "
                          "move each into its folder.")
    (tmp_path / "drive-inbox.md").write_text(good, encoding="utf-8")
    assert script.check_rewrites(tmp_path) == []
    assert script.main(["--check-rewrites", str(tmp_path)]) == 0
    (tmp_path / "drive-workspace.md").write_text(skill_markdown(
        "drive-workspace", "Use when saving files.", "Load drive-search first, then call save_file_to_drive."),
        encoding="utf-8")
    (tmp_path / "broken.md").write_text("no frontmatter", encoding="utf-8")
    problems = script.check_rewrites(tmp_path)
    assert any(p.startswith("broken.md: not a valid SKILL.md") for p in problems)
    assert {p.split("`")[1] for p in problems if p.startswith("drive-workspace.md")} == {
        "drive-search", "save_file_to_drive"}
    assert script.main(["--check-rewrites", str(tmp_path)]) == 1


def test_an_empty_rewrite_folder_is_a_problem(tmp_path):
    assert script.check_rewrites(tmp_path) == [f"no rewritten skill (*.md) in {tmp_path}"]
