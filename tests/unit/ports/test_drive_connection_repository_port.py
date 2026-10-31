"""DriveConnectionRepository (MULTI_DRIVE_RFC §3, §4.4): the abstract surface the Firestore adapter and its in-memory
twin implement (slice 1b, P1b-1): every member async and abstract, with the pinned parameters."""
import inspect

import pytest

from src.ports.drive_connection_repository import DriveConnectionRepository

_SIGNATURES = {
    "create": ["connection"],
    "get": ["user_id", "connection_id"],
    "list_for_user": ["user_id"],
    "list_by_state": ["states", "indexing_enabled"],
    "update": ["user_id", "connection_id", "credential_id", "drive_identity", "mount_name", "indexing_enabled",
               "transcribe_enabled", "provider_account"],
    "transition": ["user_id", "connection_id", "to", "now"],
    "get_settings": ["user_id"],
    "save_settings": ["settings"],
}


def test_every_member_is_abstract():
    assert set(DriveConnectionRepository.__abstractmethods__) == set(_SIGNATURES)
    with pytest.raises(TypeError):
        DriveConnectionRepository()   # type: ignore[abstract]


@pytest.mark.parametrize("name", sorted(_SIGNATURES))
def test_async_with_pinned_parameters(name):
    method = getattr(DriveConnectionRepository, name)
    assert inspect.iscoroutinefunction(method)
    assert list(inspect.signature(method).parameters)[1:] == _SIGNATURES[name]


def test_optional_filters_and_fields_are_keyword_only_and_default_to_none():
    for name, params in (("list_by_state", ["indexing_enabled"]),
                         ("update", ["credential_id", "drive_identity", "mount_name", "indexing_enabled",
                                     "transcribe_enabled", "provider_account"])):
        sig = inspect.signature(getattr(DriveConnectionRepository, name))
        for param in params:
            assert sig.parameters[param].kind is inspect.Parameter.KEYWORD_ONLY
            assert sig.parameters[param].default is None
