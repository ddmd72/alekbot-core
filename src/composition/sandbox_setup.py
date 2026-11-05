"""
Sandbox service-side composition (UNTRUSTED_CONTENT_SANDBOX_RFC §4.4, §4.6, §4.11, §4.12; plan G4, G9).

Public interface:
    validate_startup(env, *, on_cloud_run, allowed_env=frozenset()) -> StartupPlan
        every start-up refusal in one place: secret-like environment (T1), recycle policy, caller sets (T6),
        engine user. An empty service URL is not a refusal: the plan says `unconfigured` (G9, two-pass first deploy).
    build_engine_registry(plan) -> Mapping[SandboxOp, engine]
        family `render`: `render.docx` -> `DocxRenderEngine`; family `light`: the three `parse.*` ops -> `ParseEngine`, one
        instance per op, over one `ChildLauncher`. Creates/validates the output root.
    build_tunnel_registry(plan) -> Mapping[SandboxOp, tunnel engine]
        the tunnel ops (S5, `sandbox_tunnel.TUNNEL_OPS`): family `light` -> `MediaEngine`, one per op, over a launcher of
        their own (the media ceiling is not the parse one); family `render` -> none. Same output root.
    close_shared_temp_dirs(paths=SHARED_TEMP_DIRS, *, on_cloud_run, fchmod=os.fchmod) -> None
        the supervisor, before the server starts: `/tmp` and `/dev/shm` become root-only (0700, owned by this process)
        and are re-checked. Cloud Run gen2 mounts its own world-writable tmpfs over both, whatever the image set, which
        would give the engine uid writable memory outside its rlimits and its work volume. Cloud Run only: a directory
        that is missing, a symlink, or still open after the chmod refuses start. Off Cloud Run nothing is touched (one
        INFO line): a developer running the sandbox as root on a Linux host must never get the machine's /tmp closed.
        No engine child needs them: each gets TMPDIR and HOME = its own workdir (`child_launcher._child_env`).

Service family (`SANDBOX_SERVICE`): `render` (default) or `light`; any other value refuses start. The family decides the
caller groups the service accepts and the engines it builds. Render: group RENDER. Light: groups MAIN and INDEXER, both
for `parse` ops, and INDEXER also for the `media` tunnel ops (one service of its own; the render service's caller map is
separate).

It does not build the app: the blueprint, gate and recycler are `web/` objects and composition may not import `web/`
(REQ-ARCH-11); `sandbox_main.build_app` assembles them from the plan. Plain data only. Imports nothing from
`src.web`, `src.infrastructure`, `src.agents`, `src.handlers` or `src.services` (the sandbox image closure, §4.11).
The G9 first pass still needs a caller group set (`SANDBOX_CALLERS_RENDER`, or MAIN / INDEXER on the light family): an empty caller set on Cloud Run refuses start by design;
only the empty SANDBOX_URL is tolerated. Config is read from the mapping passed in (`os.getenv` knobs, never `load_settings()`).

Roots (one place, T5 reads them from the plan and passes the SAME `output_root` to the engine and the blueprint):
    work_root    SANDBOX_WORK_ROOT    default `/work` on Cloud Run (the size-limited in-memory volume the engine child
                                     may write to), else `<tmp>/sandbox_work`.
    output_root  SANDBOX_OUTPUT_ROOT  default `<tmp>/sandbox_out` (server-only, mode 0700, NOT on the work volume).
"""
from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from ..domain.sandbox_contract import SandboxOp
from ..domain.sandbox_tunnel import TUNNEL_OPS
from ..domain.sandbox_startup_check import check_sandbox_environment
from ..utils.logger import logger
from .sandbox_callers import load_caller_sets

# The gate's group -> op-prefix mapping of the render family. Group names match `SANDBOX_CALLERS_<GROUP>`.
GROUP_PREFIXES: Mapping[str, FrozenSet[str]] = {"RENDER": frozenset({"render"})}
# The light family: the main service and the indexer each get the `parse` prefix; the indexer also `media` (S5: drive
# recordings over the tunnel); MAIN also gets `media` (S5b: it probes edit-video sources).
LIGHT_GROUP_PREFIXES: Mapping[str, FrozenSet[str]] = {
    "MAIN": frozenset({"parse", "media"}), "INDEXER": frozenset({"parse", "media"}),
}

FAMILY_RENDER = "render"
FAMILY_LIGHT = "light"
_FAMILY_GROUPS: Mapping[str, Mapping[str, FrozenSet[str]]] = {
    FAMILY_RENDER: GROUP_PREFIXES, FAMILY_LIGHT: LIGHT_GROUP_PREFIXES,
}

RECYCLE_ALWAYS = "always"
RECYCLE_NEVER = "never"
RECYCLE_ABNORMAL = "abnormal"   # light (parse) service only: a normal request keeps the instance warm
# The unprivileged uid the engine child runs as on Cloud Run; the image (T9) creates this user.
DEFAULT_ENGINE_USER = "sandboxengine"
_CLOUD_RUN_WORK_ROOT = "/work"
_DEFAULT_BROWSER_CACHE_DIR = os.path.join("~", ".cache", "puppeteer")   # puppeteer's own default, expanded below
# The platform's shared temp areas: closed to everyone but the root supervisor/server before any request.
SHARED_TEMP_DIRS: Tuple[str, ...] = ("/tmp", "/dev/shm")
_SHARED_TEMP_MODE = 0o700


class SandboxStartupError(Exception):
    """Start-up refusal. Messages carry names only, never values."""


@dataclass(frozen=True)
class StartupPlan:
    recycle: str                                   # "always" | "abnormal" (light only) | "never" (local only)
    service_url: str                               # no trailing slash; "" -> unconfigured
    unconfigured: bool                             # only /health served, every op answers 503 unconfigured
    caller_sets: Mapping[str, FrozenSet[str]]      # {group: emails}
    group_prefixes: Mapping[str, FrozenSet[str]]   # {group: op prefixes}
    work_root: str
    output_root: str
    engine_user: Optional[str]                     # child uid name (None only off root, locally)
    on_cloud_run: bool
    browser_cache_dir: Optional[str] = None        # where Puppeteer finds chrome-headless-shell (render family)
    family: str = FAMILY_RENDER                    # "render" | "light" (SANDBOX_SERVICE)


def validate_startup(
    env: Mapping[str, str], *, on_cloud_run: bool, allowed_env: FrozenSet[str] = frozenset(),
) -> StartupPlan:
    if on_cloud_run != bool(env.get("K_SERVICE")):
        # load_caller_sets derives Cloud Run from K_SERVICE; the flag must agree or the rules would diverge.
        logger.error("[Sandbox] refusing to start: on_cloud_run disagrees with K_SERVICE")
        raise SandboxStartupError("on_cloud_run must equal bool(K_SERVICE)")
    violations = check_sandbox_environment(env, allowed_env)
    if violations:
        logger.error(f"[Sandbox] refusing to start: secret-like environment names: {', '.join(violations)}")
        raise SandboxStartupError(f"secret-like environment names present: {', '.join(violations)}")

    recycle = env.get("SANDBOX_RECYCLE", RECYCLE_ALWAYS)
    if recycle not in (RECYCLE_ALWAYS, RECYCLE_NEVER, RECYCLE_ABNORMAL) or (recycle == RECYCLE_NEVER and on_cloud_run):
        logger.error("[Sandbox] refusing to start: unusable SANDBOX_RECYCLE policy")
        raise SandboxStartupError("SANDBOX_RECYCLE must be 'always' or 'abnormal' ('never' is local only)")

    family = env.get("SANDBOX_SERVICE", FAMILY_RENDER)
    if family not in _FAMILY_GROUPS:
        logger.error("[Sandbox] refusing to start: unknown SANDBOX_SERVICE family")
        raise SandboxStartupError(f"SANDBOX_SERVICE must be one of {sorted(_FAMILY_GROUPS)}")
    if recycle == RECYCLE_ABNORMAL and family != FAMILY_LIGHT:
        logger.error("[Sandbox] refusing to start: policy abnormal is for the light service only")
        raise SandboxStartupError("SANDBOX_RECYCLE=abnormal is for SANDBOX_SERVICE=light only")
    group_prefixes = _FAMILY_GROUPS[family]

    caller_sets: Dict[str, FrozenSet[str]] = load_caller_sets(env)   # raises on overlap / empty on Cloud Run
    unknown = sorted(set(caller_sets) - set(group_prefixes))
    url = (env.get("SANDBOX_URL") or "").strip().rstrip("/")
    local_open = not on_cloud_run and not caller_sets and not url
    if local_open:
        # RFC §4.4: local development with nothing configured runs with the gate open (T6 builds it open).
        logger.warning("[Sandbox] no callers and no URL, not on Cloud Run: local, gate open")
    elif unknown or not set(caller_sets) & set(group_prefixes):
        logger.error(f"[Sandbox] refusing to start: caller groups {sorted(caller_sets)} do not match this service")
        raise SandboxStartupError(
            f"caller groups for the {family} service must be from {sorted(group_prefixes)}, at least one; "
            f"got unknown: {unknown or 'none, no known group'}"
        )

    if not url and not local_open:
        logger.warning("[Sandbox] SANDBOX_URL empty: starting unconfigured (only /health; ops answer 503)")

    tmp = tempfile.gettempdir()
    work_root = env.get("SANDBOX_WORK_ROOT") or (_CLOUD_RUN_WORK_ROOT if on_cloud_run else os.path.join(tmp, "sandbox_work"))
    output_root = env.get("SANDBOX_OUTPUT_ROOT") or os.path.join(tmp, "sandbox_out")
    engine_user = env.get("SANDBOX_ENGINE_USER") or (DEFAULT_ENGINE_USER if on_cloud_run else None)

    return StartupPlan(
        recycle=recycle, service_url=url, unconfigured=not url and not local_open, caller_sets=caller_sets,
        group_prefixes=dict(group_prefixes), work_root=work_root, output_root=output_root,
        engine_user=engine_user, on_cloud_run=on_cloud_run, family=family,
        browser_cache_dir=env.get("PUPPETEER_CACHE_DIR") or _DEFAULT_BROWSER_CACHE_DIR,
    )


def build_engine_registry(plan: StartupPlan) -> Mapping[SandboxOp, Any]:
    """Build the engines. Raises (never falls back) on a misconfigured engine user or output root."""
    from ..adapters.sandbox_engine.child_launcher import ChildLauncher

    output_root = _prepare_output_root(plan.output_root, plan.work_root)
    launcher = ChildLauncher(user=plan.engine_user)   # resolves/validates the user now (R6)
    if plan.family == FAMILY_LIGHT:
        from ..adapters.sandbox_engine.parse_engine import ParseEngine

        ops = (SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS)
        return {op: ParseEngine(launcher, op=op, output_root=output_root) for op in ops}
    from ..adapters.sandbox_engine.docx_render_engine import DocxRenderEngine
    from ..adapters.sandbox_engine.html_render_engine import (
        BROWSER_MEMORY_MODE, BROWSER_NOFILE, BROWSER_NPROC, HtmlRenderEngine,
    )

    # The browser cannot live under RLIMIT_AS (it reserves tens of GiB of address space): its own launcher.
    browser_launcher = ChildLauncher(
        user=plan.engine_user, memory_mode=BROWSER_MEMORY_MODE, nproc=BROWSER_NPROC, nofile=BROWSER_NOFILE,
    )
    cache_dir = os.path.expanduser(plan.browser_cache_dir or _DEFAULT_BROWSER_CACHE_DIR)
    registry: Dict[SandboxOp, Any] = {SandboxOp.RENDER_DOCX: DocxRenderEngine(launcher, output_root=output_root)}
    for op in (SandboxOp.RENDER_PDF, SandboxOp.RENDER_PNG):
        registry[op] = HtmlRenderEngine(browser_launcher, op=op, output_root=output_root, browser_cache_dir=cache_dir)
    return registry


def build_tunnel_registry(plan: StartupPlan) -> Mapping[SandboxOp, Any]:
    """The tunnel engines (`SandboxTunnelEngine`) of the plan's family. Raises (never falls back) like
    `build_engine_registry`."""
    if plan.family != FAMILY_LIGHT:
        return {}
    from ..adapters.sandbox_engine.child_launcher import ChildLauncher
    from ..adapters.sandbox_engine.media_engine import MediaEngine

    output_root = _prepare_output_root(plan.output_root, plan.work_root)
    media_launcher = ChildLauncher(user=plan.engine_user)
    return {op: MediaEngine(media_launcher, op=op, output_root=output_root)
            for op in sorted(TUNNEL_OPS, key=lambda o: o.value)}


def _prepare_output_root(output_root: str, work_root: str) -> str:
    out = os.path.realpath(output_root)
    work = os.path.realpath(work_root)
    if out == work or out.startswith(work + os.sep):
        logger.error("[Sandbox] output root lies on the engine work volume")
        raise SandboxStartupError("output root must not be inside the work root")
    try:
        info = os.lstat(output_root)
    except FileNotFoundError:
        os.makedirs(output_root, mode=0o700)
        info = os.lstat(output_root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        logger.error("[Sandbox] output root is a symlink or not a directory")
        raise SandboxStartupError("output root must be a real directory, not a symlink")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        logger.error("[Sandbox] output root is foreign-owned or writable by group/other")
        raise SandboxStartupError("output root must be owned by this process and closed to group/other")
    return out


def close_shared_temp_dirs(
    paths: Sequence[str] = SHARED_TEMP_DIRS, *, on_cloud_run: bool,
    fchmod: Callable[[int, int], None] = os.fchmod,
) -> None:
    """On Cloud Run: make each shared temp dir root-only and verify it (see the module docstring); raises
    SandboxStartupError when one does not hold. Off Cloud Run: touches nothing. `fchmod` is a test seam."""
    if not on_cloud_run:
        logger.info(f"[Sandbox] not on Cloud Run: shared temp dirs {', '.join(paths)} left as they are")
        return
    for path in paths:
        problem = _close_dir(path, fchmod)
        if problem is not None:
            logger.error(f"[Sandbox] refusing to start: shared temp dir {path} not closed ({problem})")
            raise SandboxStartupError(f"shared temp dir {path} could not be closed to the engine user: {problem}")
        logger.info(f"[Sandbox] shared temp dir {path} closed to the engine user")


def _close_dir(path: str, fchmod: Callable[[int, int], None]) -> Optional[str]:
    """None when `path` is now a real directory owned by this process with no group/other bits; else the reason.
    Works on a descriptor opened without following links, so the checked inode is the one changed."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:   # a symlink (ELOOP) or not a directory (ENOTDIR)
        return f"not a real directory ({type(exc).__name__})"
    try:
        uid = os.geteuid()
        if os.fstat(fd).st_uid != uid:
            os.fchown(fd, uid, -1)
        fchmod(fd, _SHARED_TEMP_MODE)
        info = os.fstat(fd)
    except OSError as exc:
        return f"chown/chmod refused ({type(exc).__name__})"
    finally:
        os.close(fd)
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        return "still open to group/other after the chmod"
    return None
