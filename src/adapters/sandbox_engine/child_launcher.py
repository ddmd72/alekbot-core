"""
ChildLauncher: starts one engine child for one request (RFC §4.6, §4.8, §4.14).

    ChildLauncher.run(argv, *, workdir, env, limits, user, stdin_path, stdout_path) -> ChildResult

The server never touches the payload; this launcher starts a fresh child under a separate unprivileged uid with no
supplementary groups, in its own session, with a fixed environment allowlist, stdin and stdout wired to files in the
request's directory, and the limits applied by `limits_exec` before the engine loads. The wall clock kills the whole
process group. The child's stderr is never returned or logged: only its length and SHA-256 (the log channel is
untrusted, §4.14).

A configured user that is missing, is root, or that this process cannot switch to fails the start with
ChildLaunchError: never a silent fallback (precedent `_resolve_sandbox` in subprocess_content_extractor).

One public class per file, except the small ChildResult / ChildLaunchError value types (waiver; precedent
`drive_match`). Imports nothing from other adapter subpackages and nothing under `src/infrastructure/` (REQ-ARCH-23).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pwd
import resource
import signal
import sys
import tempfile
from dataclasses import dataclass
from typing import IO, Any, Dict, Mapping, Optional, Sequence, Tuple

from ...domain.memory_telemetry import SCOPE_SUBPROCESS
from ...domain.sandbox_contract import SandboxLimits
from ...utils.logger import logger
from ...utils.memory_scope import memory_scope
from .limits_exec import MEMORY_MODE_DEFAULT, NOFILE_DEFAULT, NPROC_DEFAULT

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_LIMITS_MODULE = "src.adapters.sandbox_engine.limits_exec"

# The only names a child may see: the parent's values of these, plus what the caller passes explicitly (still only
# these names). Everything else, every application secret included, is withheld. TMPDIR and HOME are the workdir.
ENV_ALLOWLIST = ("PATH", "HOME", "NODE_PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")
_PARENT_ENV_KEYS = ("PATH", "LANG", "LC_ALL")

# Reads of the child's stderr file are chunked; only length and hash survive.
_STDERR_CHUNK = 64 * 1024


class ChildLaunchError(RuntimeError):
    """The child could not be started safely (user missing, root, or not switchable). Never retried as a fallback."""


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    timed_out: bool
    peak_rss_bytes: int      # RUSAGE_CHILDREN high-water mark: exact while the process serves one request (§4.6)
    stderr_length: int
    stderr_sha256: str


_CONSTRUCTOR_USER: Any = object()   # run(user=) default: the user the launcher was constructed with


class ChildLauncher:
    def __init__(
        self,
        *,
        python: str = sys.executable,
        nproc: int = NPROC_DEFAULT,
        nofile: int = NOFILE_DEFAULT,
        memory_mode: str = MEMORY_MODE_DEFAULT,
        user: Optional[str] = None,
    ) -> None:
        """`user` is the child uid the service is configured with. It is resolved here, once, so a misconfigured
        service fails at startup rather than on its first request. `run(user=...)` may name another one."""
        self._identity = _resolve_user(user)
        self._python = python
        self._nproc = nproc
        self._nofile = nofile
        self._memory_mode = memory_mode

    async def run(
        self,
        argv: Sequence[str],
        *,
        workdir: str,
        env: Mapping[str, str],
        limits: SandboxLimits,
        user: Optional[str] = _CONSTRUCTOR_USER,
        stdin_path: str,
        stdout_path: str,
        op: str = "child",
    ) -> ChildResult:
        identity = self._identity if user is _CONSTRUCTOR_USER else _resolve_user(user)
        child_env = _child_env(workdir, env)
        wrapped = [
            self._python, "-B", "-E", "-s", "-m", _LIMITS_MODULE,
            json.dumps({
                "memory_bytes": limits.memory_bytes, "memory_mode": self._memory_mode, "cpu_s": limits.cpu_s,
                "file_bytes": limits.file_bytes, "nproc": self._nproc, "nofile": self._nofile, "cwd": workdir,
            }),
            "--", *argv,
        ]
        with memory_scope(SCOPE_SUBPROCESS, f"sandbox_{op}"):
            return await self._spawn(wrapped, child_env, limits, identity, stdin_path, stdout_path, op)

    async def _spawn(
        self, wrapped: Sequence[str], env: Dict[str, str], limits: SandboxLimits,
        identity: Optional[Tuple[str, int]], stdin_path: str, stdout_path: str, op: str,
    ) -> ChildResult:
        kwargs: Dict[str, object] = {}
        if identity is not None:   # no supplementary groups: root's would otherwise be inherited
            kwargs = {"user": identity[0], "group": identity[1], "extra_groups": []}
        # stderr goes to an unlinked file, not a pipe: asyncio's wait() also waits for pipe EOF, which a surviving
        # grandchild would withhold. The file is bounded by the child's RLIMIT_FSIZE and read back as length + hash.
        stderr_file = tempfile.TemporaryFile()
        try:
            with open(stdin_path, "rb") as stdin_file, open(stdout_path, "wb") as stdout_file:
                proc = await asyncio.create_subprocess_exec(
                    *wrapped, stdin=stdin_file, stdout=stdout_file, stderr=stderr_file,
                    env=env, cwd=_REPO_ROOT, start_new_session=True, **kwargs,
                )
        except (OSError, ValueError) as exc:
            stderr_file.close()
            logger.error(f"[Sandbox] child op={op} could not be started: {type(exc).__name__}")
            raise
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=limits.wall_s)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            # Always, not only while the leader lives: a grandchild that outlives a clean exit would still hold the
            # result file's descriptor under the sandbox uid (the pgid stays reserved while any member lives).
            _kill_group(proc.pid)
            if proc.returncode is None:
                await proc.wait()
        stderr_length, stderr_sha256 = _summarise(stderr_file)
        stderr_file.close()
        returncode = int(proc.returncode if proc.returncode is not None else -signal.SIGKILL)
        result = ChildResult(
            returncode=returncode, timed_out=timed_out, peak_rss_bytes=_children_peak_rss_bytes(),
            stderr_length=stderr_length, stderr_sha256=stderr_sha256)
        logger.info(
            f"[Sandbox] child op={op} rc={result.returncode} timed_out={timed_out} rss={result.peak_rss_bytes} "
            f"stderr_len={result.stderr_length} stderr_sha256={result.stderr_sha256[:16]}")
        return result


# -- helpers --------------------------------------------------------------------------------------------------------

def _summarise(stream: IO[bytes]) -> Tuple[int, str]:
    """Length and SHA-256 of what the child wrote to stderr; the text is never held or returned."""
    digest = hashlib.sha256()
    length = 0
    stream.seek(0)
    while True:
        chunk = stream.read(_STDERR_CHUNK)
        if not chunk:
            return length, digest.hexdigest()
        digest.update(chunk)
        length += len(chunk)


def _child_env(workdir: str, extra: Mapping[str, str]) -> Dict[str, str]:
    refused = [name for name in extra if name not in ENV_ALLOWLIST]
    if refused:
        logger.error(f"[Sandbox] child environment names outside the allowlist refused: {sorted(refused)}")
        raise ValueError(f"environment names outside the allowlist: {sorted(refused)}")
    env = {key: value for key in _PARENT_ENV_KEYS if (value := os.environ.get(key))}
    env.update(extra)
    env["TMPDIR"] = workdir
    env["HOME"] = workdir
    return env


def _children_peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)   # Linux reports KiB, macOS bytes


def _effective_uid() -> int:
    return os.geteuid()


def _resolve_user(user: Optional[str]) -> Optional[Tuple[str, int]]:
    """(user name, gid) of the child's user, or None when none is configured (local development only)."""
    user = (user or "").strip()
    if not user:
        if _effective_uid() == 0:   # an engine as root can lift its own rlimits: never local development (I7)
            logger.error("[Sandbox] no child user configured while running as root")
            raise ChildLaunchError("no child user configured while running as root: no sandbox")
        logger.warning("[Sandbox] no child user configured: engine children run as this process's own user, with its "
                       "privileges (local development only; the service image sets one)")
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        logger.error(f"[Sandbox] child user {user!r} does not exist in this container")
        raise ChildLaunchError(f"child user {user!r} does not exist in this container") from None
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        logger.error(f"[Sandbox] child user {user!r} is root")
        raise ChildLaunchError(f"child user {user!r} is root: no sandbox")
    if _effective_uid() != 0:
        logger.error(f"[Sandbox] switching the child to {user!r} needs root")
        raise ChildLaunchError(f"running the child as {user!r} needs root")
    return user, entry.pw_gid


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        logger.debug(f"[Sandbox] child group {pid} already gone")
    except PermissionError as exc:
        logger.warning(f"[Sandbox] child group {pid} could not be signalled: {type(exc).__name__}")
