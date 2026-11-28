"""The supervisor's shared temp dirs include `/var/tmp`; an image without it is not refused, a bad one still is."""
import os
import stat

import pytest

from src.composition import sandbox_setup
from src.composition.sandbox_setup import SHARED_TEMP_DIRS, SandboxStartupError, close_shared_temp_dirs


def test_an_existing_var_tmp_is_closed(tmp_path, monkeypatch):
    var_tmp = tmp_path / "var_tmp"
    var_tmp.mkdir()
    os.chmod(var_tmp, 0o1777)
    monkeypatch.setattr(sandbox_setup, "_OPTIONAL_TEMP_DIRS", frozenset({str(var_tmp)}))
    close_shared_temp_dirs([str(var_tmp)], on_cloud_run=True)
    assert stat.S_IMODE(os.lstat(var_tmp).st_mode) == 0o700


def test_a_missing_var_tmp_is_skipped(tmp_path, monkeypatch):
    missing = tmp_path / "var_tmp"
    monkeypatch.setattr(sandbox_setup, "_OPTIONAL_TEMP_DIRS", frozenset({str(missing)}))
    close_shared_temp_dirs([str(missing)], on_cloud_run=True)


def test_a_missing_dir_that_is_not_optional_is_still_refused(tmp_path):
    with pytest.raises(SandboxStartupError):
        close_shared_temp_dirs([str(tmp_path / "shm")], on_cloud_run=True)


def test_var_tmp_is_the_only_optional_dir():
    assert sandbox_setup._OPTIONAL_TEMP_DIRS == frozenset({"/var/tmp"})
