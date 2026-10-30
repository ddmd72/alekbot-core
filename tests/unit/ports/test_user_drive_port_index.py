"""
Port contract test for the change-capture surface of UserDrivePort (USER_DRIVE_INDEXING_RFC §4).

Pins the methods the indexing tick and search call: abstract, async, and the parameter names
and defaults they are called with (`limit` required, `cursor` optional on the paged listing).
MULTI_DRIVE_RFC §6: by handle; `connection` and `flag_reconnect` left the port (state is
DriveConnectionService's).
"""
import inspect

import pytest

from src.ports.user_drive_port import UserDrivePort

_INDEX_METHODS = {
    "get_changes": (["handle", "cursor"], {}),
    "get_change": (["handle", "item_id"], {}),
    "list_children_changes": (["handle", "folder_id", "limit", "cursor"], {"cursor": None}),
}


def test_index_methods_are_abstract():
    assert set(_INDEX_METHODS) <= set(UserDrivePort.__abstractmethods__)


@pytest.mark.parametrize("name", sorted(_INDEX_METHODS))
def test_index_methods_are_async(name):
    assert inspect.iscoroutinefunction(getattr(UserDrivePort, name))


@pytest.mark.parametrize("name", sorted(_INDEX_METHODS))
def test_index_method_parameters(name):
    params, defaults = _INDEX_METHODS[name]
    signature = inspect.signature(getattr(UserDrivePort, name))
    names = [p for p in signature.parameters if p != "self"]
    assert names == params
    actual_defaults = {p.name: p.default for p in signature.parameters.values()
                       if p.default is not inspect.Parameter.empty}
    assert actual_defaults == defaults          # `limit` has no default: every caller states its page size
