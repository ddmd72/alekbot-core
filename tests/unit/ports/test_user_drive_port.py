"""
Port contract test for UserDrivePort (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Pins the abstract surface Tasks 4-6 of the step-1 plan build on: the method names, that
every operation is async (display_name is the one property), and the parameter names the
service and adapter tests call by keyword (`limit`, `new_parent_id`, `new_name`).
"""
import inspect

import pytest

from src.ports.user_drive_port import UserDrivePort

_EXPECTED = {
    "display_name", "is_connected", "disconnect", "get_root", "get_item", "list_children",
    "search", "download", "upload", "replace_content", "move", "create_folder", "delete",
}

# Positional/keyword parameter names after `self`, per method (RFC §4.2; plan Tasks 4-6).
_SIGNATURES = {
    "is_connected": ["user_id"],
    "disconnect": ["user_id"],
    "get_root": ["user_id"],
    "get_item": ["user_id", "item_id"],
    "list_children": ["user_id", "folder_id"],
    "search": ["user_id", "query", "limit"],
    "download": ["user_id", "item_id"],
    "upload": ["user_id", "parent_id", "filename", "data", "content_type"],
    "replace_content": ["user_id", "item_id", "data", "content_type"],
    "move": ["user_id", "item_id", "new_parent_id", "new_name"],
    "create_folder": ["user_id", "parent_id", "name"],
    "delete": ["user_id", "item_id"],
}


def test_port_is_abstract_with_expected_methods():
    assert _EXPECTED <= set(UserDrivePort.__abstractmethods__)


def test_cannot_instantiate_directly():
    with pytest.raises(TypeError):
        UserDrivePort()  # type: ignore[abstract]


def test_display_name_is_a_property():
    assert isinstance(inspect.getattr_static(UserDrivePort, "display_name"), property)


@pytest.mark.parametrize("name", sorted(_SIGNATURES))
def test_operations_are_async_with_pinned_parameters(name):
    method = getattr(UserDrivePort, name)
    assert inspect.iscoroutinefunction(method), f"{name} must be async"
    params = list(inspect.signature(method).parameters)[1:]
    assert params == _SIGNATURES[name]


def test_move_parent_and_name_are_optional():
    params = inspect.signature(UserDrivePort.move).parameters
    assert params["new_parent_id"].default is None
    assert params["new_name"].default is None
