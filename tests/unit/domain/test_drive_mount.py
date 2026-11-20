"""Mount: derived properties."""
from datetime import datetime, timezone

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
