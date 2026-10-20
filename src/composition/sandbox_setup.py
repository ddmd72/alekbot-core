"""
Sandbox service-side composition (UNTRUSTED_CONTENT_SANDBOX_RFC §4.4, §4.6, §4.11, §4.12; plan G4, G9).

Public interface:
    validate_startup(env, *, on_cloud_run, allowed_env=frozenset()) -> StartupPlan
        every start-up refusal in one place: secret-like environment (T1), recycle policy, caller sets (T6),
        engine user. An empty service URL is not a refusal: the plan says `unconfigured` (G9, two-pass first deploy).
    build_engine_registry(plan) -> Mapping[SandboxOp, engine]
        `render.docx` -> `DocxRenderEngine` over a `ChildLauncher`. Creates/validates the output root.

It does not build the app: the blueprint, gate and recycler are `web/` objects and composition may not import `web/`
(REQ-ARCH-11); `sandbox_main.build_app` assembles them from the plan. Plain data only. Imports nothing from
`src.web`, `src.infrastructure`, `src.agents`, `src.handlers` or `src.services` (the sandbox image closure, §4.11).
Config is read from the mapping passed in (`os.getenv` knobs, never `load_settings()`).

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
from typing import Any, Dict, FrozenSet, Mapping

from ..domain.sandbox_contract import SandboxOp
from ..domain.sandbox_startup_check import check_sandbox_environment
from ..utils.logger import logger
from .sandbox_callers import load_caller_sets

# The gate's group -> op-prefix mapping. Group names match `SANDBOX_CALLERS_<GROUP>`; later slices add rows
# (INDEXER -> drive, media; MAIN -> chat). S1 serves the render service only.
GROUP_PREFIXES: Mapping[str, FrozenSet[str]] = {"RENDER": frozenset({"render"})}

RECYCLE_ALWAYS = "always"
RECYCLE_NEVER = "never"
DEFAULT_ENGINE_USER = "sandbox"
_CLOUD_RUN_WORK_ROOT = "/work"


class SandboxStartupError(Exception):
    """Start-up refusal. Messages carry names only, never values."""


@dataclass(frozen=True)
class StartupPlan:
    recycle: str                                   # "always" | "never" (never: local only)
    service_url: str                               # no trailing slash; "" -> unconfigured
    unconfigured: bool                             # only /health served, every op answers 503 unconfigured
    caller_sets: Mapping[str, FrozenSet[str]]      # {group: emails}
    group_prefixes: Mapping[str, FrozenSet[str]]   # {group: op prefixes}
    work_root: str
    output_root: str
    engine_user: Any                               # Optional[str]: child uid name (None only off root, locally)
    on_cloud_run: bool


def validate_startup(
    env: Mapping[str, str], *, on_cloud_run: bool, allowed_env: FrozenSet[str] = frozenset(),
) -> StartupPlan:
    violations = check_sandbox_environment(env, allowed_env)
    if violations:
        logger.error(f"[Sandbox] refusing to start: secret-like environment names: {', '.join(violations)}")
        raise SandboxStartupError(f"secret-like environment names present: {', '.join(violations)}")

    recycle = env.get("SANDBOX_RECYCLE", RECYCLE_ALWAYS)
    if recycle not in (RECYCLE_ALWAYS, RECYCLE_NEVER) or (recycle == RECYCLE_NEVER and on_cloud_run):
        logger.error("[Sandbox] refusing to start: unusable SANDBOX_RECYCLE policy")
        raise SandboxStartupError("SANDBOX_RECYCLE must be 'always' ('never' is local only)")

    caller_sets: Dict[str, FrozenSet[str]] = load_caller_sets(
        {**env, "K_SERVICE": env.get("K_SERVICE") or "sandbox"} if on_cloud_run else env
    )   # raises on overlap / empty on Cloud Run
    unknown = sorted(set(caller_sets) - set(GROUP_PREFIXES))
    if unknown or "RENDER" not in caller_sets:
        logger.error(f"[Sandbox] refusing to start: caller groups {sorted(caller_sets)} do not match this service")
        raise SandboxStartupError(
            f"caller groups must be exactly {sorted(GROUP_PREFIXES)}; got unknown: {unknown or 'none, RENDER missing'}"
        )

    url = (env.get("SANDBOX_URL") or "").strip().rstrip("/")
    if not url:
        logger.warning("[Sandbox] SANDBOX_URL empty: starting unconfigured (only /health; ops answer 503)")

    tmp = tempfile.gettempdir()
    work_root = env.get("SANDBOX_WORK_ROOT") or (_CLOUD_RUN_WORK_ROOT if on_cloud_run else os.path.join(tmp, "sandbox_work"))
    output_root = env.get("SANDBOX_OUTPUT_ROOT") or os.path.join(tmp, "sandbox_out")
    engine_user = env.get("SANDBOX_ENGINE_USER") or (DEFAULT_ENGINE_USER if on_cloud_run else None)

    return StartupPlan(
        recycle=recycle, service_url=url, unconfigured=not url, caller_sets=caller_sets,
        group_prefixes=dict(GROUP_PREFIXES), work_root=work_root, output_root=output_root,
        engine_user=engine_user, on_cloud_run=on_cloud_run,
    )


def build_engine_registry(plan: StartupPlan) -> Mapping[SandboxOp, Any]:
    """Build the engines. Raises (never falls back) on a misconfigured engine user or output root."""
    from ..adapters.sandbox_engine.child_launcher import ChildLauncher
    from ..adapters.sandbox_engine.docx_render_engine import DocxRenderEngine

    output_root = _prepare_output_root(plan.output_root, plan.work_root)
    launcher = ChildLauncher(user=plan.engine_user)   # resolves/validates the user now (R6)
    return {SandboxOp.RENDER_DOCX: DocxRenderEngine(launcher, output_root=output_root)}


def _prepare_output_root(output_root: str, work_root: str) -> str:
    out = os.path.abspath(output_root)
    work = os.path.abspath(work_root)
    if out == work or out.startswith(work + os.sep):
        logger.error("[Sandbox] output root lies on the engine work volume")
        raise SandboxStartupError("output root must not be inside the work root")
    try:
        info = os.lstat(out)
    except FileNotFoundError:
        os.makedirs(out, mode=0o700)
        info = os.lstat(out)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        logger.error("[Sandbox] output root is a symlink or not a directory")
        raise SandboxStartupError("output root must be a real directory, not a symlink")
    return out
