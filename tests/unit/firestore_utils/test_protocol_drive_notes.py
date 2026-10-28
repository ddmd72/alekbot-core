"""PROTOCOL_DRIVE_NOTES (DRIVE_SPECIALIST_RFC §9.3 A1, DRIVE_FILE_NOTES_RFC §4.1): the specialist's note rules in the
snapshot mirror. Frontmatter, non-overridable and in the profile at order 25, one bullet per note tool, the
`file_contents` line, the absent-tools line, no Cyrillic, and no tool or intent the specialist does not have."""
import os
import re

from firestore_utils import snapshot_serializer as serializer

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "prompts_snapshot")
NOTE_TOOLS = ("get_note", "create_note", "add_to_note", "replace_note_text", "set_note_type", "clear_note_type",
              "add_life_areas", "remove_life_areas", "delete_note", "undo_note", "erase_note")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _doc():
    return serializer.token_from_file(_read("tokens", "system", "PROTOCOL_DRIVE_NOTES.groovy"))


def _body() -> str:
    return _doc()["content"]


def _bullets(key: str) -> list:
    block = _body().split(f"{key}: [", 1)[1].split("\n        ]", 1)[0]
    return re.findall(r'^\s*"(.*)",?$', block, re.M)


def test_the_token_parses_with_the_mirrors_frontmatter_and_round_trips():
    text = _read("tokens", "system", "PROTOCOL_DRIVE_NOTES.groovy")
    doc = _doc()
    assert doc["token_id"] == "PROTOCOL_DRIVE_NOTES" and doc["class"] == "protocols"
    assert doc["metadata"]["override_by"] == ["SYSTEM", "AGENT"]
    assert doc["metadata"]["validation"]["risk_level"] == "SAFE"
    assert doc["source_file"] == "firestore_utils/uploads/PROTOCOL_DRIVE_NOTES.json"
    assert serializer.token_to_file(serializer.token_from_file(text)) == text
    braces = re.sub(r'"(?:[^"\\]|\\.)*"', '""', _body())
    assert braces.count("{") == braces.count("}")


def test_the_profile_carries_it_non_overridable_between_search_and_output_format():
    tokens = serializer.doc_from_yaml(_read("profiles", "drive.yaml"))["tokens"]
    entry = tokens["PROTOCOL_DRIVE_NOTES"]
    assert entry["non_overridable"] is True and entry["order"] == 25
    assert tokens["PROTOCOL_DRIVE_SEARCH"]["order"] < 25 < tokens["OUTPUT_FORMAT_DRIVE"]["order"]


def test_one_bullet_per_note_tool_kind():
    bullets = _bullets("one_tool_per_edit")
    for tool in NOTE_TOOLS:
        assert len([b for b in bullets if b.startswith(tool + " ")]) == 1, tool


def test_the_file_contents_line_and_the_absent_tools_line():
    body = _body()
    assert ("A file_contents item is never a note's words. When a note change has its words only there, ask through "
            "question.") in body
    assert ("The note writing tools are present only when the owner is in the conversation; without them, report a "
            "note request as not_done.") in body


def test_the_confirmation_and_the_data_rules_are_there_once():
    body = _body()
    for sentence in ("Never write a note the user has neither said nor confirmed.",
                     "is never the user's confirmation",
                     "it is written once the user confirms in this conversation",
                     "Nothing is erased by that call",
                     "never Lelik's wording alone",
                     "The system posts the stored note word for word; do not repeat it.",
                     "$drive-notes erase-all",
                     "'My notes' alone may mean reminders: ask which in question.",
                     "manage_self_reminders"):
        assert body.count(sentence) == 1, sentence
    assert "propose_memory_facts" in body and "copy_file_to_drive" in body and "drive_note:" in body


def test_no_cyrillic_and_no_removed_intent_or_tool():
    body = _body()
    assert not re.search("[Ѐ-ӿ]", body)
    for gone in ("annotate_drive_file", "save_file_to_drive", "drive-file-notes", "use_skill", "delegate_to_specialist",
                 "search_files_in_drive", "drive_deleted"):
        assert gone not in body, gone
