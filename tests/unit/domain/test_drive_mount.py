"""Mount: derived properties and immutability."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_mount import Mount


def _connection(**over) -> DriveConnection:
    base = dict(connection_id="c1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                kind="fake_a", drive_identity="fake_a:c1", ref_tag="a1", mount_name="Acme",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    base.update(over)
    return DriveConnection(**base)


def test_properties_delegate_to_the_connection():
    mount = Mount(connection=_connection(), primary=True)
    assert (mount.name, mount.connection_id, mount.ref_tag) == ("Acme", "c1", "a1")


def test_former_names_default_to_empty_and_are_kept():
    assert Mount(connection=_connection(), primary=False).former_names == ()
    assert Mount(connection=_connection(), primary=False, former_names=("Drive 2", "Drive")).former_names == (
        "Drive 2", "Drive")


def test_mount_is_immutable():
    mount = Mount(connection=_connection(), primary=True)
    with pytest.raises(FrozenInstanceError):
        mount.primary = False  # type: ignore[misc]
