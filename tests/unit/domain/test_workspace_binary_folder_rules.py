"""The binary_files/ folder in the path rule, the kinds and the tool declaration (AGENT_WORKSPACE_RFC D10, §6, §7.1-7.2)."""
import pytest

from src.domain.exceptions import WorkspaceRefused
from src.domain.workspace import BINARY_FOLDER, FOLDERS, TEXT_FOLDERS, folder_of, is_text, parse_workspace_path
from src.infrastructure.agent_manifest import FILE_MANAGEMENT, FILE_REF_DESCRIPTION, Intent
from src.infrastructure.workspace_tools import build_workspace_tool_declaration


def test_the_folders():
    assert FOLDERS == ("projects/", "references/", "scratch/", "binary_files/")
    assert BINARY_FOLDER == "binary_files/" and BINARY_FOLDER not in TEXT_FOLDERS


def test_binary_files_paths_are_accepted():
    assert parse_workspace_path("binary_files/lease.pdf") == "binary_files/lease.pdf"
    assert parse_workspace_path("workspace:binary_files/scan.pdf") == "binary_files/scan.pdf"
    assert parse_workspace_path("binary_files/", allow_folder=True) == "binary_files/"
    assert folder_of("binary_files/lease.pdf") == BINARY_FOLDER


def test_the_old_folder_is_an_unknown_start_folder():
    with pytest.raises(WorkspaceRefused) as e:
        parse_workspace_path("files/lease.pdf")
    assert "start with projects/, references/, scratch/ or binary_files/" in str(e.value)


def test_a_bare_name_points_at_the_new_folder():
    with pytest.raises(WorkspaceRefused) as e:
        parse_workspace_path("lease.pdf")
    assert "a binary file: under binary_files/" in str(e.value) and "projects/, references/ or scratch/" in str(e.value)


def test_a_non_utf8_text_extension_file_is_not_text():
    assert is_text("projects/a.md", b"ok") and not is_text("projects/a.md", b"\xff\xfe")


class TestDeclaration:
    def _props(self):
        return build_workspace_tool_declaration()["parameters"]["properties"]

    def test_the_description_names_the_folder_rule(self):
        d = build_workspace_tool_declaration()["description"]
        assert ("binary_files/ (the only place for binary files — PDF, images, scans: copy puts them there byte for "
                "byte; you cannot create or edit them and grep does not search them; look at one with open_file and "
                "the ref workspace:binary_files/<name>); text files go to the other folders (copy accepts a text file "
                "for any of them).") in d
        assert " files/ " not in d and "workspace:files" not in d

    def test_the_parameter_texts(self):
        props = self._props()
        assert "binary_files/" in props["path"]["description"]
        assert "binary_files/ stays in binary_files/" in props["new_path"]["description"]
        source = props["source"]["description"]
        assert "drive file ref exactly as the drive tool returned it" in source
        assert "binary_files/, which holds binary files only" in source

    def test_the_pattern_text_explains_line_anchors(self):
        text = self._props()["pattern"]["description"]
        assert "start and end of a LINE" in text and "^T1" in text


def test_the_open_file_manifest_names_the_new_ref():
    assert "workspace:binary_files/<name>" in FILE_MANAGEMENT.capability_descriptions[Intent.OPEN_FILE]
    # reviewer (U4a, plan U4 §6): `file_ref` has one neutral description in every descriptor; it names the workspace
    # store, the capability description above keeps the binary_files form
    described = FILE_MANAGEMENT.context_schemas[Intent.OPEN_FILE]["file_ref"]
    assert described == FILE_REF_DESCRIPTION and "workspace:" in described
