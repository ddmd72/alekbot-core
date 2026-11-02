"""The four Cabinet settings on `DriveConnection` (MULTI_DRIVE_RFC §11.3, D4, D11; design §1.5, E-1)."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_policy import scope_key
from src.domain.drive_connection import ROOT_FOLDER, DriveConnection
from src.domain.drive_setting_refused import DriveSettingRefused
from src.domain.user_drive import DriveAccessRefused

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def connection(**over):
    base = dict(connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
                drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0)
    base.update(over)
    return DriveConnection(**base)


def test_defaults_are_the_migrated_connection():
    c = connection()
    assert c.write_folder_id == ROOT_FOLDER == ""
    assert c.searched_by_default is True
    assert c.scope_folder_ids == () and c.scope_whole_drive is False
    assert c.scope_key == ""


def test_read_only_is_none():
    assert connection(write_folder_id=None).write_folder_id is None


def test_folders_and_the_whole_drive_together_are_refused():
    with pytest.raises(ValueError):
        connection(scope_folder_ids=("f-rd",), scope_whole_drive=True)
    with pytest.raises(ValueError):
        replace(connection(scope_whole_drive=True), scope_folder_ids=("f-rd",))


def test_a_scope_folder_id_is_a_non_empty_string():
    with pytest.raises(ValueError):
        connection(scope_folder_ids=("",))


def test_scope_key_is_the_policys():
    assert connection(scope_whole_drive=True).scope_key == "whole"
    folders = connection(scope_folder_ids=("f-rd", "f-coimbra"))
    assert folders.scope_key == scope_key(["f-coimbra", "f-rd"], False) != ""


def test_the_refusals_carry_their_line():
    assert DriveSettingRefused("a line").line == "a line"
    assert DriveAccessRefused("a line").line == "a line"
