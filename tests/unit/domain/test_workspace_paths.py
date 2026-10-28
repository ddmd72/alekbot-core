"""Path rule, kinds, generations and reminder-field values (AGENT_WORKSPACE_RFC §6, §7.2, §8.3)."""
from datetime import datetime, timezone

import pytest

from src.domain.exceptions import WorkspaceConflict, WorkspaceRefused
from src.domain.workspace import (
    BINDING_INSTRUCTION_NOTE,
    BINDING_NOT_APPLIED,
    BINDING_ONE_REMINDER,
    COMMANDS,
    MUTATING_COMMANDS,
    ReminderField,
    binding_system_line,
    fire_key,
    folder_of,
    is_text,
    parse_generation,
    parse_reminder_field,
    parse_workspace_path,
    validate_workspace_file,
    workspace_kind,
)


class TestPathRule:
    @pytest.mark.parametrize("raw,expected", [
        ("projects/garden-shed.md", "projects/garden-shed.md"),
        ("workspace:projects/garden-shed.md", "projects/garden-shed.md"),
        ("  references/where_things_live.txt ", "references/where_things_live.txt"),
        ("scratch/a/b/draft.v2.md", "scratch/a/b/draft.v2.md"),
        ("files/lease.pdf", "files/lease.pdf"),
    ])
    def test_valid_file_paths(self, raw, expected):
        assert parse_workspace_path(raw) == expected

    @pytest.mark.parametrize("raw,rule", [
        ("Projects/a.md", "lowercase"),
        ("projects/a:b.md", "lowercase"),
        ("projects/../x.md", "'..' are not allowed"),
        ("projects/./x.md", "'..' are not allowed"),
        ("projects/.hidden.md", "starts with a letter or digit"),
        ("/projects/a.md", "no leading '/'"),
        ("projects//a.md", "no '//'"),
        ("notes/a.md", "start with projects/, references/, scratch/ or files/"),
        ("projects/a", "needs an extension"),
        ("projects/a/b/c/d.md", "at most 4 segments"),
        ("projects/" + "a" * 120 + ".md", "at most 120 characters"),
        ("projects/a.md/", "no trailing '/'"),
        ("", "a path is required"),
    ])
    def test_invalid_paths_name_the_rule(self, raw, rule):
        with pytest.raises(WorkspaceRefused) as e:
            parse_workspace_path(raw)
        assert rule in str(e.value)
        assert str(e.value).startswith("invalid workspace path")
        assert e.value.reason == "path"

    @pytest.mark.parametrize("raw,tool", [
        ("drive:01ABC", "work_with_drive_files"),
        ("skill:flight-status/references/airlines.md", "open_file"),
        ("report (1).pdf", "copy"),
    ])
    def test_refs_of_other_stores_name_the_right_tool(self, raw, tool):
        with pytest.raises(WorkspaceRefused) as e:
            parse_workspace_path(raw)
        assert tool in str(e.value)
        assert e.value.reason == "wrong_store"

    @pytest.mark.parametrize("raw,expected", [
        ("", ""),
        ("workspace:", ""),
        ("projects", "projects/"),
        ("projects/", "projects/"),
        ("workspace:projects/garden/", "projects/garden/"),
        ("projects/garden", "projects/garden/"),
        ("projects/garden-shed.md", "projects/garden-shed.md"),
    ])
    def test_folders_for_view_and_history(self, raw, expected):
        assert parse_workspace_path(raw, allow_folder=True) == expected

    def test_unknown_folder_is_refused_even_for_view(self):
        with pytest.raises(WorkspaceRefused):
            parse_workspace_path("notes/", allow_folder=True)


class TestKinds:
    @pytest.mark.parametrize("path,kind", [
        ("projects/a.md", "text"), ("references/r.txt", "text"), ("files/data.csv", "text"),
        ("files/t.tsv", "text"), ("files/c.json", "text"), ("files/c.yaml", "text"), ("files/c.yml", "text"),
        ("files/lease.pdf", "binary"), ("files/photo.jpg", "binary"),
    ])
    def test_kind_from_extension(self, path, kind):
        assert workspace_kind(path) == kind

    def test_text_needs_utf8_too(self):
        assert is_text("files/a.txt", "привіт".encode("utf-8"))
        assert not is_text("files/a.txt", b"\xff\xfe\x00bad")
        assert not is_text("files/a.pdf", b"plain")

    def test_folder_of(self):
        assert folder_of("projects/a/b.md") == "projects/"
        assert folder_of("files/lease.pdf") == "files/"


class TestCommands:
    def test_eight_commands_six_mutating(self):
        assert COMMANDS == ("view", "create", "str_replace", "delete", "rename", "copy", "history", "restore")
        assert MUTATING_COMMANDS == {"create", "str_replace", "delete", "rename", "copy", "restore"}


class TestGeneration:
    @pytest.mark.parametrize("raw", [None, "", " ", "none", "NULL", "n/a", "0", 0])
    def test_not_given(self, raw):
        assert parse_generation(raw) is None

    @pytest.mark.parametrize("raw,expected", [("101", 101), (" 102 ", 102), (103, 103)])
    def test_given(self, raw, expected):
        assert parse_generation(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "-5", "1.5", True])
    def test_invalid(self, raw):
        with pytest.raises(WorkspaceRefused) as e:
            parse_generation(raw)
        assert e.value.reason == "generation"


class TestFireKey:
    def test_shape(self):
        due = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
        assert fire_key("1729401234567", due) == "reminder:1729401234567@2026-10-20T06:00:00+00:00"


class TestReminderField:
    @pytest.mark.parametrize("raw", ["remove", "REMOVE", " Remove "])
    def test_remove_clears(self, raw):
        assert parse_reminder_field(raw) == ReminderField(clear=True)
        assert parse_reminder_field(raw).provided

    @pytest.mark.parametrize("raw", [None, "", "  ", "none", "None", "null", "NULL", "n/a", "N/A"])
    def test_fillers_mean_not_provided(self, raw):
        assert parse_reminder_field(raw) == ReminderField()
        assert not parse_reminder_field(raw).provided

    def test_value(self):
        assert parse_reminder_field(" projects/morning-briefing.md ") == ReminderField(
            value="projects/morning-briefing.md")


class TestWorkspaceFile:
    @pytest.mark.parametrize("raw,expected", [
        ("projects/morning-briefing.md", "projects/morning-briefing.md"),
        ("workspace:references/sources.txt", "references/sources.txt"),
        ("scratch/draft.md", "scratch/draft.md"),       # any folder (D17); scratch expires (§10 row 12)
        ("files/lease.pdf", "files/lease.pdf"),         # any kind: what the run does with it is the instruction's
    ])
    def test_any_valid_path(self, raw, expected):
        assert validate_workspace_file(raw) == expected

    @pytest.mark.parametrize("raw", ["Morning Briefing", "projects/../x.md", "drive:01ABC", "projects/"])
    def test_an_invalid_path_names_the_field(self, raw):
        with pytest.raises(WorkspaceRefused) as e:
            validate_workspace_file(raw)
        assert str(e.value).startswith("workspace_file: ")
        assert e.value.reason == "workspace_file"


class TestBindingLine:
    def test_attach(self):
        assert binding_system_line(ReminderField(value="projects/morning-briefing.md")) == (
            "System: workspace_file=projects/morning-briefing.md will be attached by code to the reminder you create "
            "or update in this request. Call update_self_reminder with its note_id even if nothing else changes. "
            "Keep the instruction short and let it refer to the file."
        )

    def test_remove(self):
        assert binding_system_line(ReminderField(clear=True)) == (
            "System: the workspace file will be removed by code from the reminder you update in this request. Call "
            "update_self_reminder with its note_id even if nothing else changes."
        )

    def test_texts(self):
        assert BINDING_INSTRUCTION_NOTE == (
            "When a workspace file is attached to the reminder, keep this short and let it refer to the file.")
        assert BINDING_ONE_REMINDER == (
            "workspace_file applies to one reminder per request; create or update exactly one")
        assert BINDING_NOT_APPLIED == "workspace_file was not applied"


def test_exceptions():
    e = WorkspaceRefused("x is wrong", reason="path")
    assert str(e) == "x is wrong" and e.reason == "path"
    assert WorkspaceRefused("y").reason == "refused"
    assert isinstance(WorkspaceConflict("projects/a.md"), Exception)
