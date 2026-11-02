"""The shadowing warning needs a live primary to name; without one the name is valid and carries no warning."""
from datetime import datetime, timezone

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mounts import validate_mount_name

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _conn(connection_id, name, tag, state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name, created_at=T0,
                           state=state, disconnected_at=T0 if state is ConnectionState.DISCONNECTED else None)


def test_no_primary_means_no_warning():
    result = validate_mount_name("Acme", connection_id=None, connections=[_conn("conn-1", "Drive", "a1")],
                                 primary_connection_id=None, primary_top_folders=["Acme"], now=T0)
    assert (result.ok, result.warning) == (True, "")


def test_a_disconnected_primary_means_no_warning():
    gone = _conn("conn-1", "Drive", "a1", ConnectionState.DISCONNECTED)
    result = validate_mount_name("Acme", connection_id=None, connections=[gone],
                                 primary_connection_id="conn-1", primary_top_folders=["Acme"], now=T0)
    assert (result.ok, result.warning) == (True, "")
