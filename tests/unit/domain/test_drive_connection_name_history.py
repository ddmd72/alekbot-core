"""`DriveConnection.name_history` (MULTI_DRIVE_RFC §11.8; slice 2a core): empty is valid (a connection stored before the
field), and a non-empty history ends with the current name, compared by `mount_name_key`. Technical ids only."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_mount_name_change import MountNameChange

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def conn(name="Drive", history=()):
    return DriveConnection(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                           kind="fake_a", drive_identity="fake_a:conn-1", ref_tag="a1", mount_name=name,
                           created_at=T0, name_history=tuple(history))


def test_an_empty_history_is_valid_and_the_default():
    assert conn().name_history == ()


def test_a_history_ending_with_the_current_name():
    history = (MountNameChange("Acme", T0), MountNameChange("Acme Hamburg", T1))
    assert conn("Acme Hamburg", history).name_history == history


def test_the_last_entry_compares_by_key():
    assert conn("Acme  Hamburg", [MountNameChange("acme hamburg", T1)]).mount_name == "Acme  Hamburg"


def test_a_history_ending_with_another_name_is_refused():
    with pytest.raises(ValueError):
        conn("Acme Hamburg", [MountNameChange("Acme Hamburg", T0), MountNameChange("Acme", T1)])


def test_replace_applies_the_rule():
    stored = conn("Drive", [MountNameChange("Drive", T0)])
    with pytest.raises(ValueError):
        replace(stored, mount_name="Acme")
    renamed = replace(stored, mount_name="Acme", name_history=stored.name_history + (MountNameChange("Acme", T1),))
    assert [c.name for c in renamed.name_history] == ["Drive", "Acme"]


def test_a_name_change_is_frozen():
    change = MountNameChange("Drive", T0)
    with pytest.raises(AttributeError):
        change.name = "Acme"   # type: ignore[misc]
