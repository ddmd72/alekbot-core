"""`send_file_to_user` in the manifest (EXTERNAL_FILES_RFC D8): a SYNC FileManagement intent whose schema is `file_ref` and
`file_name` (never `name`), and not a native-opening intent."""
from src.domain.drive_tool_texts import COPY_FILE_NAME
from src.infrastructure.agent_manifest import (
    DRIVE_SPECIALIST, FILE_MANAGEMENT, FILE_REF_DESCRIPTION, NATIVE_OPENING_INTENTS, Intent,
)
from src.infrastructure.agent_registry import ExecutionMode


def test_the_intent_is_sync_on_file_management():
    assert Intent.SEND_FILE_TO_USER == "send_file_to_user"
    assert FILE_MANAGEMENT.capabilities[Intent.SEND_FILE_TO_USER] is ExecutionMode.SYNC
    assert Intent.SEND_FILE_TO_USER in FILE_MANAGEMENT.capability_descriptions


def test_the_schema_is_file_ref_and_file_name_only():
    schema = FILE_MANAGEMENT.context_schemas[Intent.SEND_FILE_TO_USER]
    assert schema == {"file_ref": FILE_REF_DESCRIPTION, "file_name": COPY_FILE_NAME}
    assert "name" not in schema


def test_it_is_not_a_native_opening_intent():
    assert Intent.SEND_FILE_TO_USER not in NATIVE_OPENING_INTENTS


def test_send_names_email_and_drive_refs_as_the_inputs_it_needs():
    text = FILE_MANAGEMENT.capability_descriptions[Intent.SEND_FILE_TO_USER]
    assert "email:" in text and "drive:" in text and "get_email_details" in text


def test_the_drive_specialist_says_it_does_not_send_to_the_chat():
    text = DRIVE_SPECIALIST.capability_descriptions[Intent.WORK_WITH_DRIVE_FILES]
    assert "does not send files to the user's chat" in text and Intent.SEND_FILE_TO_USER in text
