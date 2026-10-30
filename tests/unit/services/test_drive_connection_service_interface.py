"""DriveConnectionService's frozen interface (MULTI_DRIVE_RFC §4.4, §9; slice 1b C1b-3): the members and parameters the
parts call, and the domain types `resolve_ref` answers with. The behaviour is P1b-1's, tested with its implementation."""
import inspect
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_ref_refusal import (
    DriveRefRefusal, disconnected_ref_line, stale_ref_line,
)
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

SIGNATURES = {
    "activate": ["credential", "drive_identity"],
    "get": ["user_id", "connection_id"],
    "list_for_user": ["user_id", "include_disconnected"],
    "handle_for": ["user_id", "connection_id"],
    "primary": ["user_id"],
    "primary_handle": ["user_id"],
    "set_primary": ["user_id", "connection_id"],
    "status": ["user_id"],
    "resolve_ref": ["user_id", "text"],
    "set_indexing": ["user_id", "connection_id", "enabled"],
    "set_transcribe": ["user_id", "connection_id", "enabled"],
    "flag_reconnect": ["handle"],
    "disconnect": ["user_id", "connection_id"],
    "reconnect_needed": ["user_id"],
}


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_async_members_with_pinned_parameters(name):
    method = getattr(DriveConnectionService, name)
    assert inspect.iscoroutinefunction(method)
    assert list(inspect.signature(method).parameters)[1:] == SIGNATURES[name]


def test_the_public_members_are_exactly_the_frozen_set():
    public = {n for n, v in vars(DriveConnectionService).items() if not n.startswith("_") and callable(v)}
    assert public == set(SIGNATURES)


def test_it_builds_over_the_port():
    service = DriveConnectionService(FakeDriveConnectionRepository(), credentials=None)   # type: ignore[arg-type]
    assert isinstance(service, DriveConnectionService)


class TestRefAnswers:
    def test_the_two_lines(self):
        assert stale_ref_line("drive:01ABC") == (
            "Ref drive:01ABC is from before the drives were reorganised; search for the file again.")
        assert disconnected_ref_line("drive:a1:item-1") == (
            "Ref drive:a1:item-1 belongs to a drive that is no longer connected.")
        assert DriveRefRefusal(line="x").line == "x"

    def test_a_resolved_ref_carries_the_connection_handle(self):
        conn = DriveConnection(connection_id="conn-1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                               kind="fake_a", drive_identity="fake_a:drive-1", ref_tag="a1",
                               created_at=datetime(2026, 10, 30, tzinfo=timezone.utc))
        resolved = ResolvedDriveRef(connection=conn, item_id="item-1")
        assert resolved.handle == conn.handle
        with pytest.raises(ValueError):
            ResolvedDriveRef(connection=conn, item_id="")
