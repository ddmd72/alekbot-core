"""
Port contract test for the change-capture surface of UserDrivePort (USER_DRIVE_INDEXING_RFC §4).

Pins the five methods the indexing tick and search call: abstract, async, and the parameter names
and defaults they are called with (`limit` required, `cursor` optional on the paged listing).
"""
import inspect

import pytest

from src.ports.user_drive_port import UserDrivePort

_INDEX_METHODS = {
    "get_changes": (["user_id", "cursor"], {}),
    "get_change": (["user_id", "item_id"], {}),
    "list_children_changes": (["user_id", "folder_id", "limit", "cursor"], {"cursor": None}),
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
