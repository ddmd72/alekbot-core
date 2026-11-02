"""PathTarget: construction, defaults, immutability."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_mount import Mount
from src.domain.drive_path_target import PathTarget


def _mount() -> Mount:
    conn = DriveConnection(connection_id="c1", user_id="usr-1", account_id="acc-1", credential_id="cred-1",
                           kind="fake_a", drive_identity="fake_a:c1", ref_tag="a1", mount_name="Acme",
                           created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    return Mount(connection=conn, primary=False)


def test_notice_defaults_to_empty():
    target = PathTarget(mount=_mount(), path="Docs/a.pdf", named=True)
    assert (target.path, target.named, target.notice) == ("Docs/a.pdf", True, "")
    assert target.mount.name == "Acme"


def test_root_path_and_notice_are_kept():
    target = PathTarget(mount=_mount(), path="", named=True, notice="renamed")
    assert target.path == "" and target.notice == "renamed"


def test_is_immutable():
    target = PathTarget(mount=_mount(), path="", named=False)
    with pytest.raises(FrozenInstanceError):
        target.path = "x"  # type: ignore[misc]
