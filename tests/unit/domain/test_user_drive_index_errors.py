"""Errors and the connection value the indexing tick and search read (USER_DRIVE_INDEXING_RFC §4, §7.7)."""
from src.domain.user_drive import (
    DriveAccessDenied, DriveConnection, DriveNotConnectedError, DriveReauthRequired, DriveResyncRequired,
    DriveThrottled,
)


def test_reauth_is_a_not_connected_error_so_step1_callers_keep_their_handling():
    assert issubclass(DriveReauthRequired, DriveNotConnectedError)


def test_access_denied_is_not_a_lost_grant():
    assert not issubclass(DriveAccessDenied, DriveNotConnectedError)


def test_resync_and_throttle_carry_their_data():
    assert DriveResyncRequired("cursor-x").restart_cursor == "cursor-x"
    assert DriveThrottled(45.0).retry_after_s == 45.0


def test_connection_defaults():
    conn = DriveConnection(connected=True)
    assert conn.needs_reconnect is False and conn.drive_id == ""
