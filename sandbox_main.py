"""
Sandbox service entrypoint (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.6, §4.11, §4.12, §4.14; plan G4, G7, G9).

One file, two processes (like `indexer_main.py` it is the only place the layers meet, REQ-ARCH-11):

    python sandbox_main.py            SUPERVISOR (the container's PID 1). Validates the environment, creates the work
                                      root, starts `python sandbox_main.py --serve` as a child in its own process
                                      group with the write end of a pipe, and waits. After the one request of the
                                      instance (policy `always`) it SIGTERMs the child, kills what is left of the
                                      child's group and exits 0 so the platform replaces the instance. The supervisor
                                      and recycler modules import no payload code; this file itself loads Quart,
                                      Hypercorn and the app modules (engines are imported only by the server).
    python sandbox_main.py --serve    SERVER. Quart on Hypercorn (HTTP/2 prior knowledge, which is what Cloud Run
                                      speaks to a `--use-http2` service, and HTTP/1.1). Assembles the blueprint, the
                                      caller gate and the pipe recycler. SIGTERM: Hypercorn finishes the in-flight
                                      response, then the process exits.

Environment (optional knobs, read with `os.getenv`-style mapping access, never `load_settings()`; the service holds no
secret): SANDBOX_URL (own URL = OIDC audience; empty -> `unconfigured`, G9), SANDBOX_CALLERS_RENDER, SANDBOX_RECYCLE
(`always` default | `never`, local only), SANDBOX_WORK_ROOT, SANDBOX_OUTPUT_ROOT, SANDBOX_ENGINE_USER, PORT, and the
platform's K_SERVICE. The server child inherits the supervisor's environment unchanged plus `SANDBOX_RECYCLE_FD`:
the gate must see K_SERVICE or it would be OPEN on Cloud Run.

Imports nothing from `src/infrastructure/`, agents, services, handlers or Firestore (image closure, §4.11).
"""
from __future__ import annotations

import asyncio
import os
import signal
import stat
import subprocess
import sys
from typing import Any, Callable, List, Mapping, Optional, Sequence

from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig
from quart import Quart

from src.composition.sandbox_callers import SandboxCallersError
from src.composition.sandbox_setup import (
    RECYCLE_ALWAYS,
    SandboxStartupError,
    StartupPlan,
    build_engine_registry,
    validate_startup,
)
from src.domain.sandbox_contract import (
    CEILING_CPU_S,
    CEILING_FILE_BYTES,
    CEILING_MEMORY_BYTES,
    CEILING_WALL_S,
    SandboxLimits,
)
from src.utils.logger import logger
from src.web.sandbox_app import create_sandbox_blueprint
from src.web.sandbox_caller_gate import SandboxCallerGate
from src.web.sandbox_recycler import PipeRecycler
from src.web.sandbox_supervisor import SandboxSupervisor
from src.web.sandbox_unconfigured_app import create_unconfigured_blueprint

RECYCLE_FD_ENV = "SANDBOX_RECYCLE_FD"
# The one name the official python base image defines that the startup self-check's pattern reads as secret-shaped
# (`GPG_KEY`: the public id of the key that signed the Python release, set by the image). An allowlist entry, never a
# looser pattern (RFC §4.3; tests/unit/test_dockerfile_sandbox.py).
IMAGE_ENV_ALLOWLIST = frozenset({"GPG_KEY"})
# Hypercorn's wait for the in-flight response after SIGTERM, then leftovers are cancelled. Cloud Run allows ~10 s
# between its own SIGTERM and SIGKILL; the supervisor's SIGKILL grace (self-triggered recycles) is longer.
GRACEFUL_TIMEOUT_S = 8.0
_SUPERVISOR_KILL_GRACE_S = 15.0
_WORK_ROOT_MODE = 0o711
_EXIT_REFUSED = 2

_CEILING = SandboxLimits(
    wall_s=CEILING_WALL_S, memory_bytes=CEILING_MEMORY_BYTES, cpu_s=CEILING_CPU_S, file_bytes=CEILING_FILE_BYTES,
)


# ---- server ---------------------------------------------------------------------------------------------------

def build_app(env: Mapping[str, str], recycler: Optional[Any]) -> Quart:
    """Assemble the `--serve` app from the environment. Raises SandboxStartupError on any refusal."""
    plan = validate_startup(env, on_cloud_run=bool(env.get("K_SERVICE")), allowed_env=IMAGE_ENV_ALLOWLIST)
    app = Quart(__name__)
    if plan.unconfigured:
        # G9 first pass. The engine registry is deliberately not built: nothing can run, and an engine-user problem
        # must not fail the deploy step that exists to learn the service URL.
        app.register_blueprint(create_unconfigured_blueprint())
        logger.warning("[Sandbox] unconfigured: only /health is served")
        return app
    if recycler is None and plan.recycle == RECYCLE_ALWAYS:
        logger.error("[Sandbox] refusing to start: policy always without the supervisor's pipe")
        raise SandboxStartupError("--serve under policy 'always' must be started by the supervisor")
    engines = build_engine_registry(plan)
    gate = SandboxCallerGate(plan.caller_sets, plan.group_prefixes, plan.service_url)
    app.register_blueprint(create_sandbox_blueprint(
        engines, gate, recycler if recycler is not None else PipeRecycler(None), _CEILING,
        {
            "work_root": plan.work_root,
            "engine_user": plan.engine_user,
            "recycle": plan.recycle,
            # the SAME root the engine registry resolved (it uses the real path)
            "output_root": os.path.realpath(plan.output_root),
        },
    ))
    logger.info("[Sandbox] ready (recycle=%s, cloud_run=%s)", plan.recycle, plan.on_cloud_run)
    return app


async def run_server(app: Any, env: Mapping[str, str] = os.environ, *, host: str = "0.0.0.0",
                     graceful_timeout_s: float = GRACEFUL_TIMEOUT_S) -> None:
    """Serve until SIGTERM/SIGINT, then let in-flight work finish for at most `graceful_timeout_s` and cancel the rest."""
    config = HypercornConfig()
    config.bind = [f"{host}:{int(env.get('PORT', '8080'))}"]
    config.accesslog = None
    config.errorlog = "-"
    config.graceful_timeout = graceful_timeout_s
    # Hypercorn serves HTTP/2 over cleartext on prior knowledge (the connection preface) next to HTTP/1.1 by default.
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    await serve_until_shutdown(app, config, shutdown)


async def serve_until_shutdown(app: Any, config: Any, shutdown: asyncio.Event) -> None:
    serve_task = asyncio.create_task(serve(app, config, shutdown_trigger=shutdown.wait))
    signalled = asyncio.create_task(shutdown.wait())
    await asyncio.wait({serve_task, signalled}, return_when=asyncio.FIRST_COMPLETED)
    if serve_task.done():
        signalled.cancel()
        serve_task.result()    # a server that stopped on its own (bind failure) raises here
        return
    await asyncio.wait({serve_task}, timeout=config.graceful_timeout)
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if leftover:
        logger.warning("[Sandbox] graceful timeout (%.0f s) passed; cancelling %d task(s)",
                       config.graceful_timeout, len(leftover))
    for task in leftover:
        task.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)


def _recycler_from_env(env: Mapping[str, str]) -> Optional[PipeRecycler]:
    raw = env.get(RECYCLE_FD_ENV)
    if not raw:
        return None
    try:
        return PipeRecycler(int(raw))
    except (ValueError, OSError) as exc:
        logger.error("[Sandbox] unusable %s (%s)", RECYCLE_FD_ENV, type(exc).__name__)
        raise SandboxStartupError(f"{RECYCLE_FD_ENV} is not a usable pipe descriptor") from exc


def bind_host(plan: StartupPlan) -> str:
    """Loopback when the gate is open (local, no callers, no URL): engines run as the developer's uid, so an open
    endpoint must not be reachable from the LAN."""
    return "127.0.0.1" if not plan.on_cloud_run and not plan.caller_sets and not plan.service_url else "0.0.0.0"


def serve_main(env: Mapping[str, str]) -> int:
    try:
        plan = validate_startup(env, on_cloud_run=bool(env.get("K_SERVICE")), allowed_env=IMAGE_ENV_ALLOWLIST)
        app = build_app(env, _recycler_from_env(env))
    except (SandboxStartupError, SandboxCallersError) as exc:
        logger.error("[Sandbox] server refused to start: %s", exc)
        return _EXIT_REFUSED
    asyncio.run(run_server(app, env, host=bind_host(plan)))
    return 0


# ---- supervisor -----------------------------------------------------------------------------------------------

def make_spawn(argv: Sequence[str], env: Mapping[str, str]) -> Callable[[int], int]:
    """A spawn function for `SandboxSupervisor`: the child inherits `env` unchanged plus the pipe's fd number, and gets
    its own session so the supervisor can end what is left of its group."""
    def spawn(write_fd: int) -> int:
        child_env = dict(env)
        child_env[RECYCLE_FD_ENV] = str(write_fd)
        proc = subprocess.Popen(list(argv), env=child_env, pass_fds=(write_fd,), start_new_session=True)
        # The supervisor collects the status itself with os.waitpid (it may be PID 1 and reap strangers too);
        # mark the Popen as finished so it neither waits nor warns.
        proc.returncode = 0
        return proc.pid
    return spawn


def prepare_work_root(plan: StartupPlan) -> None:
    """Create the engine work volume mount point: traversable (x) but not listable or writable by others."""
    path = plan.work_root
    os.makedirs(path, mode=_WORK_ROOT_MODE, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        logger.error("[Sandbox] work root is a symlink or not a directory")
        raise SandboxStartupError("work root must be a real directory, not a symlink")
    if info.st_uid != os.geteuid():
        logger.error("[Sandbox] work root is foreign-owned")
        raise SandboxStartupError("work root must be owned by this process")
    # 0711, not 0755: the engine child must traverse to its own 0700 request directory by name, and has no need to
    # list the request ids of others; group/other can never write here, so only the server creates entries.
    os.chmod(path, _WORK_ROOT_MODE)


def supervise(env: Mapping[str, str], *, install_signals: bool = True) -> int:
    try:
        plan = validate_startup(env, on_cloud_run=bool(env.get("K_SERVICE")), allowed_env=IMAGE_ENV_ALLOWLIST)
        prepare_work_root(plan)
    except (SandboxStartupError, SandboxCallersError) as exc:
        logger.error("[Sandbox] refusing to start: %s", exc)
        return _EXIT_REFUSED
    except OSError as exc:
        logger.error("[Sandbox] refusing to start: work root unusable (%s)", type(exc).__name__)
        return _EXIT_REFUSED
    argv: List[str] = [sys.executable, os.path.abspath(__file__), "--serve"]
    supervisor = SandboxSupervisor(plan.recycle, make_spawn(argv, env), kill_grace_s=_SUPERVISOR_KILL_GRACE_S)
    if install_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _signum, _frame: supervisor.terminate())
    return supervisor.run()


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None, *,
         install_signals: bool = True) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if env is None else env
    if "--serve" in args:
        return serve_main(environment)
    return supervise(environment, install_signals=install_signals)


if __name__ == "__main__":
    sys.exit(main())
