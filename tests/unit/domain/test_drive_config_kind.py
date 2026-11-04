"""Config files (`.yaml .yml .toml .ini`, policy 5): kind `config`, admitted for content and handled like plain text."""
from dataclasses import replace

import pytest

from src.domain.document_kind import kind_of, sniff_kind
from src.domain.drive_card import _KIND_LABEL
from src.domain.drive_index import (
    NAME_KEYS_VERSION, POLICY_VERSION, ContentClass, DriveIndexRecord, Reason, RecordState, Scope,
)
from src.domain.drive_index_planner import change_from_record, classify, structure_stale
from src.domain.drive_index_policy import Admission, admission, content_class_of
from src.domain.drive_profile import policy_of
from src.domain.drive_search import canonical_kind, kinds_for
from src.domain.drive_text_gate import is_machine_text


@pytest.mark.parametrize("name", ["Acme/team.yaml", "Acme/team.YML", "Acme/offices.toml", "settings.INI", "a.Yaml"])
def test_config_extensions_are_config(name):
    assert kind_of(name, "", is_folder=False) == "config"


@pytest.mark.parametrize("name", ["Acme/offices.xml", "tool.py", "style.css", "app.js"])
def test_source_code_and_xml_stay_code(name):
    assert kind_of(name, "", is_folder=False) == "code"


@pytest.mark.parametrize("mime", ["text/plain", "application/octet-stream", "text/x-yaml", ""])
def test_a_declared_mime_does_not_change_config(mime):
    assert kind_of("Acme/team.yaml", mime, is_folder=False) == "config"


def test_a_text_head_stays_config_and_a_binary_head_is_other():
    assert sniff_kind(b"owner: Lukas Weber\noffice: Hamburg\n", "config") == "config"
    assert sniff_kind(b"owner: \x00\x00\x01 Lukas", "config") == "other"


def test_config_is_admitted_as_a_document():
    assert admission("config") is Admission.CONTENT
    assert content_class_of("config") is ContentClass.DOCUMENT


def test_a_config_file_is_planned_for_a_content_read():
    from src.domain.drive_index import DriveChange
    change = DriveChange(item_id="a", name="Acme/team.yaml", parent_id="root", size_bytes=1000)
    cls = classify(change, Scope.NORMAL, None, None)
    assert (cls.kind, cls.state, cls.reason) == ("config", RecordState.PENDING, None)


def test_a_yaml_stored_under_policy_4_is_re_planned_as_config():
    """Policy 5: a `.yaml` stored before it as `code / kind_not_admitted` is stale; the re-plan makes it a pending read."""
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", parent_id="root", name="team.yaml",
                           size_bytes=1000, kind="code", scope=Scope.NORMAL, state=RecordState.METADATA_ONLY,
                           reason=Reason.KIND_NOT_ADMITTED, policy_version=4, name_keys_version=NAME_KEYS_VERSION)
    assert POLICY_VERSION == 5
    assert structure_stale(rec, embedding_space=None)
    assert not structure_stale(replace(rec, policy_version=POLICY_VERSION), embedding_space=None)
    cls = classify(change_from_record(rec), Scope.NORMAL, None, rec)
    assert (cls.kind, cls.state, cls.reason) == ("config", RecordState.PENDING, None)


def test_the_parser_reads_a_yaml_file_as_text(tmp_path):
    from src.adapters.drive_parser import readers
    path = tmp_path / "team.yaml"
    path.write_text("lead: Lukas Weber\noffice: Hamburg\n", encoding="utf-8")
    text = readers._READERS["config"](str(path), "config")[0]
    assert "lead: Lukas Weber" in text and "office: Hamburg" in text


def test_a_config_is_opened_in_process_like_text():
    from src.domain.document_read import IN_PROCESS_TEXT_KINDS
    assert "config" in IN_PROCESS_TEXT_KINDS


@pytest.mark.parametrize("chars", [10, 100, 400, 5000, 50000])
def test_config_has_the_profile_policy_of_text(chars):
    assert policy_of("config", text_chars=chars) == policy_of("text", text_chars=chars)
    assert policy_of("config", text_chars=chars, machine_text=True) == policy_of("text", text_chars=chars,
                                                                                  machine_text=True)


def test_config_has_a_card_label():
    assert _KIND_LABEL["config"] == "structured text (YAML, TOML, INI)"


def test_kind_config_is_accepted_by_the_search_request():
    assert canonical_kind("config") == "config"
    assert canonical_kind("yaml") == "config"
    assert kinds_for("config") == frozenset({"config"})


def test_the_kind_field_text_names_config():
    from src.domain.drive_tool_texts import KIND
    assert "csv, config, image" in KIND


_TEAMS = ("finance", "logistics", "design", "support", "sales", "research", "legal", "operations")
_OFFICES = ("Porto", "Hamburg", "Lviv")
_MANAGERS = ("Lukas Weber", "Tom Baker", "Taras Melnyk")
_NOTES = ("is reviewed every quarter by the finance group", "must match the name printed on the signed order",
          "is shared between two offices during the planning season", "defaults to the value agreed with the supplier",
          "is copied into the yearly report for the board", "changes only after the manager approves the request",
          "is checked against the invoice before payment", "belongs to the team that opened the project",
          "moves to the next team when the project ends", "is listed in the welcome guide for new colleagues")


def test_a_team_roster_yaml_is_not_machine_text():
    entries = []
    for i in range(100):
        team, note = _TEAMS[i % 8], _NOTES[(i // 8) % 10]   # 100 distinct (team, note, office, manager) combinations
        entries.append(
            f"- owner: team_{i}\n  office: {_OFFICES[(i // 3) % 3]}\n  team: {team}\n"
            f"  manager: {_MANAGERS[(i // 9) % 3]}\n  note: The {team} group number {i} {note}, as the {_TEAMS[(i // 80 + 3) % 8]} asked."
        )
    assert not is_machine_text("\n".join(entries))