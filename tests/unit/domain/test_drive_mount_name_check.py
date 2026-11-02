"""MountNameCheck: `ok` is exactly "no error"."""
from dataclasses import FrozenInstanceError

import pytest

from src.domain.drive_mount_name_check import MountNameCheck


def test_valid_name_is_ok_and_may_carry_a_warning():
    check = MountNameCheck(name="Acme", warning="shadows Acme Hamburg")
    assert check.ok is True
    assert check.error == ""
    assert check.warning == "shadows Acme Hamburg"


def test_error_makes_it_not_ok():
    check = MountNameCheck(name="Drive", error="taken")
    assert check.ok is False
    assert check.warning == ""


def test_is_immutable():
    with pytest.raises(FrozenInstanceError):
        MountNameCheck(name="Drive").name = "Drive 2"  # type: ignore[misc]
