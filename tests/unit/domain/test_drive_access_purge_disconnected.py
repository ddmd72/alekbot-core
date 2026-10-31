"""The gate's `PURGE_DISCONNECTED` outcome (MULTI_DRIVE_RFC §8, I8): a connection that is gone (DISCONNECTED or absent)
with something stored purges its index; with nothing stored there is nothing to do. Pure: no repository, no clock."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import DriveSyncState
from src.domain.drive_index_planner import AccessAction, access_action

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def _connection(state=ConnectionState.ACTIVE, indexing=True):
    live = DriveConnection(connection_id="c1", user_id="u", account_id="acc-1", credential_id="cred-1",
                           kind="microsoft_onedrive", drive_identity="d1", ref_tag="a1", indexing_enabled=indexing,
                           created_at=NOW)
    if state is ConnectionState.DISCONNECTED:
        return replace(live, state=state, disconnected_at=NOW)
    return replace(live, state=state)


STORED = DriveSyncState(user_id="u", connection_id="c1", drive_identity="d1")


def test_a_disconnected_connection_with_a_stored_index_is_purged():
    assert access_action(STORED, _connection(ConnectionState.DISCONNECTED), NOW) is AccessAction.PURGE_DISCONNECTED


def test_an_absent_connection_with_a_stored_index_is_purged():
    assert access_action(STORED, None, NOW) is AccessAction.PURGE_DISCONNECTED


@pytest.mark.parametrize("connection", [None, ConnectionState.DISCONNECTED])
def test_a_gone_connection_with_nothing_stored_is_a_no_op(connection):
    conn = _connection(connection) if connection else None
    assert access_action(None, conn, NOW) is AccessAction.NOOP_DISABLED


def test_the_disconnect_wins_over_the_indexing_switch():
    gone = _connection(ConnectionState.DISCONNECTED, indexing=False)
    assert access_action(STORED, gone, NOW) is AccessAction.PURGE_DISCONNECTED
