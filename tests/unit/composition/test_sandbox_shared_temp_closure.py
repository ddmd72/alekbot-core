"""The supervisor closes the shared temp dirs (`/tmp`, `/dev/shm`) to the engine uid before the server starts
(decisions/sandbox_render_service.md). Cloud Run gen2 mounts its own world-writable tmpfs over both, whatever the image
set. Every test works on directories under `tmp_path`; the machine's own `/tmp` is never touched."""
import os
import stat

import pytest

import sandbox_main
from src.composition.sandbox_setup import SHARED_TEMP_DIRS, SandboxStartupError, close_shared_temp_dirs

CALLER = "caller@example.invalid"


def _open_dir(path):
    path.mkdir()
    os.chmod(path, 0o1777)   # what the platform's tmpfs mount looks like
    return path


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def _no_op_fchmod(fd, mode):
    """A chmod that silently does nothing: the verification, not the call, must catch it."""


def _refused_fchmod(fd, mode):
    raise PermissionError("not permitted")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def test_the_closed_set_is_tmp_and_dev_shm():
    assert SHARED_TEMP_DIRS == ("/tmp", "/dev/shm")


@pytest.mark.parametrize("on_cloud_run", [True, False])
def test_open_dirs_become_owner_only_and_owned_by_this_process(tmp_path, on_cloud_run):
    tmp, shm = _open_dir(tmp_path / "tmp"), _open_dir(tmp_path / "shm")
    close_shared_temp_dirs([str(tmp), str(shm)], on_cloud_run=on_cloud_run)
    for path in (tmp, shm):
        assert _mode(path) == 0o700            # sticky and world bits gone
        assert os.lstat(path).st_uid == os.geteuid()


def test_an_already_closed_dir_stays_closed(tmp_path):
    path = tmp_path / "tmp"
    path.mkdir(mode=0o700)
    close_shared_temp_dirs([str(path)], on_cloud_run=True)
    assert _mode(path) == 0o700


def test_on_cloud_run_a_chmod_that_does_not_hold_refuses_start(tmp_path):
    path = _open_dir(tmp_path / "tmp")
    with pytest.raises(SandboxStartupError, match="still open"):
        close_shared_temp_dirs([str(path)], on_cloud_run=True, fchmod=_no_op_fchmod)
    assert _mode(path) == 0o1777


def test_on_cloud_run_a_refused_chmod_refuses_start(tmp_path):
    path = _open_dir(tmp_path / "tmp")
    with pytest.raises(SandboxStartupError, match="refused"):
        close_shared_temp_dirs([str(path)], on_cloud_run=True, fchmod=_refused_fchmod)


def test_on_cloud_run_a_missing_dir_refuses_start(tmp_path):
    tmp = _open_dir(tmp_path / "tmp")
    with pytest.raises(SandboxStartupError, match="missing"):
        close_shared_temp_dirs([str(tmp), str(tmp_path / "shm")], on_cloud_run=True)


def test_on_cloud_run_a_symlink_refuses_start_and_its_target_is_not_changed(tmp_path):
    target = _open_dir(tmp_path / "elsewhere")
    link = tmp_path / "tmp"
    os.symlink(target, link)
    with pytest.raises(SandboxStartupError, match="not a real directory"):
        close_shared_temp_dirs([str(link)], on_cloud_run=True)
    assert _mode(target) == 0o1777


def test_on_cloud_run_a_later_dir_is_still_checked_after_the_first_closed(tmp_path):
    tmp = _open_dir(tmp_path / "tmp")
    not_a_dir = tmp_path / "shm"
    not_a_dir.write_bytes(b"")
    with pytest.raises(SandboxStartupError, match="shm"):
        close_shared_temp_dirs([str(tmp), str(not_a_dir)], on_cloud_run=True)


def test_off_cloud_run_a_missing_dev_shm_is_logged_and_the_rest_still_closed(tmp_path, caplog):
    """macOS has no /dev/shm: local development starts anyway."""
    tmp = _open_dir(tmp_path / "tmp")
    close_shared_temp_dirs([str(tmp_path / "shm"), str(tmp)], on_cloud_run=False)
    assert _mode(tmp) == 0o700
    assert any("shm" in m and "missing" in m for m in _warnings(caplog)), _warnings(caplog)


@pytest.mark.parametrize("fchmod, reason", [(_no_op_fchmod, "still open"), (_refused_fchmod, "refused")])
def test_off_cloud_run_a_closure_that_does_not_hold_is_logged_not_fatal(tmp_path, caplog, fchmod, reason):
    path = _open_dir(tmp_path / "tmp")
    close_shared_temp_dirs([str(path)], on_cloud_run=False, fchmod=fchmod)
    assert any(reason in m for m in _warnings(caplog)), _warnings(caplog)


def test_off_cloud_run_a_symlinked_tmp_is_left_alone(tmp_path, caplog):
    """macOS: /tmp is a symlink to /private/tmp; the closure never follows it."""
    target = _open_dir(tmp_path / "private_tmp")
    link = tmp_path / "tmp"
    os.symlink(target, link)
    close_shared_temp_dirs([str(link)], on_cloud_run=False)
    assert _mode(target) == 0o1777
    assert any("not a real directory" in m for m in _warnings(caplog)), _warnings(caplog)


# -- the supervisor applies it before the server child exists ------------------------------------------------------

@pytest.fixture
def cloud_env(tmp_path):
    return {
        "PATH": os.environ.get("PATH", ""),
        "K_SERVICE": "svc",
        "SANDBOX_URL": "https://sandbox.example.invalid",
        "SANDBOX_CALLERS_RENDER": CALLER,
        "SANDBOX_WORK_ROOT": str(tmp_path / "work"),
        "SANDBOX_OUTPUT_ROOT": str(tmp_path / "out"),
    }


def test_supervisor_closes_the_shared_dirs_before_starting_the_server(cloud_env, tmp_path, monkeypatch):
    tmp, shm = _open_dir(tmp_path / "tmp"), _open_dir(tmp_path / "shm")
    monkeypatch.setattr(sandbox_main, "SHARED_TEMP_DIRS", (str(tmp), str(shm)))
    seen = {}

    class Fake:
        def __init__(self, *a, **k):
            seen["modes_at_start"] = (_mode(tmp), _mode(shm))

        def run(self):
            return 0

        def terminate(self):
            pass

    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", Fake)
    assert sandbox_main.supervise(cloud_env, install_signals=False) == 0
    assert seen["modes_at_start"] == (0o700, 0o700)


def test_supervisor_on_cloud_run_refuses_start_when_a_shared_dir_cannot_be_closed(cloud_env, tmp_path, monkeypatch):
    tmp = _open_dir(tmp_path / "tmp")
    monkeypatch.setattr(sandbox_main, "SHARED_TEMP_DIRS", (str(tmp), str(tmp_path / "missing_shm")))
    monkeypatch.setattr(sandbox_main, "SandboxSupervisor", lambda *a, **k: pytest.fail("must not start"))
    assert sandbox_main.supervise(cloud_env, install_signals=False) != 0
