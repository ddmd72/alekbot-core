"""The parser child runs as an unprivileged user (R3 F3-1, USER_DRIVE_INDEXING_RFC §9, §24.14): spawned with that
uid/gid and no supplementary groups, in a per-run directory it owns, reading a group-readable link of the source and
writing its window there for the parent to collect. A child of another uid cannot read the parent's
/proc/<ppid>/environ or rewrite the app source. Without a configured user (local development) it runs as this
process's own user, and says so in a WARNING; a configured user that is missing, is root, or cannot be switched to
(not root) fails the start.

A real switch of uid needs root, so the sandboxed spawn is recorded and then run as the test's own user, with the
sandbox user resolved to the test's own uid/gid."""
import asyncio
import logging
import os
import stat
from types import SimpleNamespace

import pytest

from src.adapters import subprocess_content_extractor as module
from src.adapters.subprocess_content_extractor import PARSER_USER_ENV, SubprocessContentExtractor
from src.domain.drive_extraction import ExtractionRequest, ExtractionStatus
from tests.unit.adapters.drive_extraction_fixtures import make_pdf

UID, GID = os.getuid(), os.getgid()


def _request(path, kind):
    return ExtractionRequest(path=str(path), name=os.path.basename(str(path)), kind=kind, mime_type="",
                             user_id="u1", account_id="a1")


def _workroot(tmp_path):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def as_root_with_parser_user(monkeypatch):
    """`driveparser` resolves to this test's own uid/gid, and the process looks like root."""
    def getpwnam(name):
        if name != "driveparser":
            raise KeyError(name)
        return SimpleNamespace(pw_name=name, pw_uid=UID, pw_gid=GID)

    monkeypatch.setattr(module.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(module, "_effective_uid", lambda: 0)


@pytest.fixture
def recorded_spawns(monkeypatch):
    """Records each spawn's arguments and what the child will see, then runs it as this test's own user."""
    spawns = []
    real = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        workdir = kwargs["env"]["TMPDIR"]
        spawns.append({"kwargs": dict(kwargs), "workdir_stat": os.stat(workdir),
                       "files": {name: os.stat(os.path.join(workdir, name)) for name in os.listdir(workdir)}})
        for key in ("user", "group", "extra_groups"):
            kwargs.pop(key, None)
        return await real(*args, **kwargs)

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", record)
    return spawns


# =============================================================================
# Spawn arguments and the per-run directory
# =============================================================================

async def test_sandboxed_child_runs_as_the_parser_user_without_supplementary_groups(
        tmp_path, as_root_with_parser_user, recorded_spawns):
    source = tmp_path / "note.txt"
    source.write_text("Penalty is 0.1% per day.")
    extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)), sandbox_user="driveparser")
    outcome = await extractor.extract(_request(source, "text"))
    assert outcome.status is ExtractionStatus.TEXT and "Penalty is 0.1% per day." in outcome.text
    (spawn,) = recorded_spawns
    kwargs = spawn["kwargs"]
    assert (kwargs["user"], kwargs["group"], kwargs["extra_groups"]) == (UID, GID, [])
    assert kwargs["start_new_session"] is True
    workdir = kwargs["env"]["TMPDIR"]
    assert kwargs["env"]["HOME"] == workdir                       # no HOME of the parent's user
    assert (spawn["workdir_stat"].st_uid, spawn["workdir_stat"].st_gid) == (UID, GID)
    assert stat.S_IMODE(spawn["workdir_stat"].st_mode) == 0o700
    staged = spawn["files"]["source.txt"]                        # the child reads a link inside its own directory
    assert staged.st_gid == GID and stat.S_IMODE(staged.st_mode) == 0o440
    assert list(_workroot(tmp_path).iterdir()) == []             # removed afterwards


async def test_sandboxed_cut_is_written_in_the_child_directory_and_collected(
        tmp_path, as_root_with_parser_user, recorded_spawns):
    source = make_pdf(tmp_path / "s.pdf", [f"page {i}" for i in range(1, 6)])
    windows = tmp_path / "windows"
    windows.mkdir()
    extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)), sandbox_user="driveparser")
    assert await extractor.pdf_page_count(str(source)) == 5
    await extractor.cut_pdf_window(str(source), 0, 3, str(windows / "w.pdf"))
    assert os.listdir(windows) == ["w.pdf"] and stat.S_ISREG(os.lstat(windows / "w.pdf").st_mode)
    assert all(s["kwargs"]["user"] == UID for s in recorded_spawns)
    assert list(_workroot(tmp_path).iterdir()) == []


async def test_unsandboxed_child_gets_no_user_arguments(tmp_path, recorded_spawns, caplog):
    source = tmp_path / "note.txt"
    source.write_text("hello")
    with caplog.at_level(logging.WARNING):
        extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)))
    assert "own user" in caplog.text and PARSER_USER_ENV in caplog.text   # explicit, never silent
    outcome = await extractor.extract(_request(source, "text"))
    assert outcome.status is ExtractionStatus.TEXT
    kwargs = recorded_spawns[0]["kwargs"]
    assert "user" not in kwargs and "group" not in kwargs and "extra_groups" not in kwargs


# =============================================================================
# The fallback rule: a configured user is enforced, never silently dropped
# =============================================================================

def test_a_configured_user_missing_from_the_container_fails_the_start(as_root_with_parser_user):
    with pytest.raises(RuntimeError, match="does not exist"):
        SubprocessContentExtractor(sandbox_user="nobody-here")


def test_a_configured_user_that_is_root_fails_the_start(monkeypatch):
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: SimpleNamespace(pw_name=name, pw_uid=0, pw_gid=0))
    monkeypatch.setattr(module, "_effective_uid", lambda: 0)
    with pytest.raises(RuntimeError, match="root"):
        SubprocessContentExtractor(sandbox_user="driveparser")


def test_a_configured_user_without_root_to_switch_fails_the_start(as_root_with_parser_user, monkeypatch):
    monkeypatch.setattr(module, "_effective_uid", lambda: 1000)
    with pytest.raises(RuntimeError, match="needs root"):
        SubprocessContentExtractor(sandbox_user="driveparser")


def test_a_blank_user_is_no_user(caplog):
    with caplog.at_level(logging.WARNING):
        SubprocessContentExtractor(sandbox_user="  ")
    assert "own user" in caplog.text


# =============================================================================
# The parent reads what the child leaves only as a regular file, never through a link it planted
# =============================================================================

def test_result_that_is_a_link_is_not_read(tmp_path):
    target = tmp_path / "secret.json"
    target.write_text('{"status": "text", "text": "parent-only"}')
    link = tmp_path / "result.json"
    os.symlink(target, link)
    assert module._load_result(str(link)) is None


def test_result_that_is_a_fifo_is_not_read_and_does_not_block(tmp_path):
    fifo = tmp_path / "result.json"
    os.mkfifo(fifo)
    assert module._load_result(str(fifo)) is None
