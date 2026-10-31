"""
Port contract test for UserDrivePort (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Pins the abstract surface Tasks 4-6 of the step-1 plan build on: the method names, that
every operation is async, and the parameter names the service and adapter tests call by keyword
(`limit`, `new_parent_id`, `new_name`). MULTI_DRIVE_RFC §6: every method takes a `DriveHandle` where
it took `user_id`; `display_name`, `is_connected` and `disconnect` left the port (labels to the
registry, state to DriveConnectionService).
"""
import inspect

import pytest

from src.ports.user_drive_port import UserDrivePort

_EXPECTED = {
    "get_root", "get_item", "list_children",
    "search", "download", "upload", "replace_content", "move", "create_folder", "delete",
}

# Positional/keyword parameter names after `self`, per method (RFC §4.2; plan Tasks 4-6; MULTI_DRIVE_RFC §6).
_SIGNATURES = {
    "get_root": ["handle"],
    "get_item": ["handle", "item_id"],
    "list_children": ["handle", "folder_id"],
    "search": ["handle", "query", "limit"],
    "download": ["handle", "item_id"],
    "upload": ["handle", "parent_id", "filename", "data", "content_type", "conflict"],
    "replace_content": ["handle", "item_id", "data", "content_type"],
    "move": ["handle", "item_id", "new_parent_id", "new_name"],
    "create_folder": ["handle", "parent_id", "name"],
    "delete": ["handle", "item_id"],
}


def test_port_is_abstract_with_expected_methods():
    assert _EXPECTED <= set(UserDrivePort.__abstractmethods__)


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        UserDrivePort()  # type: ignore[abstract]


@pytest.mark.parametrize("name", sorted(_SIGNATURES))
def test_operations_are_async_with_pinned_parameters(name):
    method = getattr(UserDrivePort, name)
    assert inspect.iscoroutinefunction(method), f"{name} must be async"
    params = list(inspect.signature(method).parameters)[1:]
    assert params == _SIGNATURES[name]


def test_upload_conflict_defaults_to_rename():
    # USER_DRIVE_INDEXING_RFC §27.9 (T2 Task 18): every Step 1 caller passes no mode and keeps "never overwrites".
    assert inspect.signature(UserDrivePort.upload).parameters["conflict"].default == "rename"


def test_move_parent_and_name_are_optional():
    params = inspect.signature(UserDrivePort.move).parameters
    assert params["new_parent_id"].default is None
    assert params["new_name"].default is None
