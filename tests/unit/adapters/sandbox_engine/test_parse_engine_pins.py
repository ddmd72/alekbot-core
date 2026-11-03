"""Constants the ParseEngine mirrors from modules an engine may not import (REQ-ARCH-23): pinned in step."""
from src.adapters import subprocess_content_extractor as in_process
from src.adapters.drive_parser import child as drive_child
from src.adapters.sandbox_engine import child_launcher, parse_child, parse_engine
from src.domain.sandbox_parse_result import PARSE_RESULT_MAX_BYTES


def test_exit_codes_and_child_env_are_pinned_to_the_in_process_adapter_and_the_child():
    assert (parse_engine.EXIT_MEMORY, parse_engine.EXIT_BAD_REQUEST) == (drive_child.EXIT_MEMORY, drive_child.EXIT_BAD_REQUEST)
    assert dict(parse_engine.CHILD_ENV) == in_process._FIXED_ENV
    assert set(parse_engine.CHILD_ENV) <= set(child_launcher.ENV_ALLOWLIST)


def test_the_child_module_the_entry_script_starts_is_the_drive_child():
    assert parse_child.DRIVE_CHILD_MODULE == in_process.CHILD_MODULE == "src.adapters.drive_parser.child"


def test_result_bound_is_the_in_process_adapters():
    assert PARSE_RESULT_MAX_BYTES == in_process._RESULT_MAX_BYTES
