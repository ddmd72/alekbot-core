"""T7: service-side sandbox composition (RFC §4.4, §4.6, §4.11, §4.12; plan G4, G9)."""
import ast
import os
import pathlib

import pytest

from src.composition import sandbox_setup
from src.composition.sandbox_callers import SandboxCallersError
from src.composition.sandbox_setup import (
    GROUP_PREFIXES, SandboxStartupError, StartupPlan, build_engine_registry, validate_startup,
)
from src.domain.sandbox_contract import SandboxOp

_OK = {"SANDBOX_CALLERS_RENDER": "main@example.com", "SANDBOX_RECYCLE": "always"}


def _plan(extra=None, on_cloud_run=False, **kw):
    env = {**_OK, **(extra or {})}
    if on_cloud_run:
        env.setdefault("K_SERVICE", "svc")
    return validate_startup(env, on_cloud_run=on_cloud_run, **kw)


def test_validate_startup_returns_plan_fields():
    plan = _plan({"SANDBOX_URL": "https://svc.example.com/", "SANDBOX_OUTPUT_ROOT": "/o", "SANDBOX_WORK_ROOT": "/w"})
    assert isinstance(plan, StartupPlan)
    assert plan.recycle == "always"
    assert plan.service_url == "https://svc.example.com"
    assert plan.unconfigured is False
    assert plan.caller_sets == {"RENDER": frozenset({"main@example.com"})}
    assert plan.group_prefixes == {"RENDER": frozenset({"render"})} == GROUP_PREFIXES
    assert (plan.work_root, plan.output_root) == ("/w", "/o")


def test_validate_startup_policy_never_allowed_locally_only():
    assert _plan({"SANDBOX_RECYCLE": "never"}).recycle == "never"


@pytest.mark.parametrize("policy", ["abnormal", "", "sometimes"])
def test_validate_startup_rejects_unknown_policy(policy):
    with pytest.raises(SandboxStartupError):
        _plan({"SANDBOX_RECYCLE": policy})


def test_validate_startup_policy_defaults_to_always():
    env = {"SANDBOX_CALLERS_RENDER": "main@example.com"}
    assert validate_startup(env, on_cloud_run=False).recycle == "always"


def test_validate_startup_never_refused_on_cloud_run():
    with pytest.raises(SandboxStartupError):
        _plan({"SANDBOX_RECYCLE": "never", "SANDBOX_URL": "https://x.example.com"}, on_cloud_run=True)


def test_validate_startup_overlap_refused():
    with pytest.raises(SandboxCallersError):
        _plan({"SANDBOX_CALLERS_OTHER": "main@example.com"})


def test_validate_startup_empty_set_on_cloud_run_refused():
    with pytest.raises(SandboxCallersError):
        validate_startup({"SANDBOX_CALLERS_RENDER": "", "K_SERVICE": "svc"}, on_cloud_run=True)


def test_validate_startup_unknown_group_refused():
    with pytest.raises(SandboxStartupError):
        _plan({"SANDBOX_CALLERS_MYSTERY": "z@example.com"})


def test_validate_startup_missing_render_group_refused():
    with pytest.raises(SandboxStartupError):
        validate_startup({"SANDBOX_RECYCLE": "always", "SANDBOX_URL": "https://svc.example.com"}, on_cloud_run=False)


def test_validate_startup_empty_url_on_cloud_run_is_unconfigured_not_refusal():
    plan = _plan(on_cloud_run=True)
    assert plan.unconfigured is True and plan.service_url == ""


def test_validate_startup_empty_url_locally_is_also_unconfigured():
    assert _plan().unconfigured is True


def test_validate_startup_secret_like_env_refused_without_values():
    with pytest.raises(SandboxStartupError) as err:
        _plan({"OPENAI_API_KEY": "sk-very-secret"})
    assert "OPENAI_API_KEY" in str(err.value)
    assert "sk-very-secret" not in str(err.value)


def test_validate_startup_allowlisted_env_name_passes():
    _plan({"MY_TOKEN": "x"}, allowed_env=frozenset({"MY_TOKEN"}))


def test_validate_startup_engine_user_default_cloud_run_and_local():
    assert _plan({"SANDBOX_URL": "https://x"}, on_cloud_run=True).engine_user == "sandboxengine"
    assert _plan().engine_user is None
    assert _plan({"SANDBOX_ENGINE_USER": "eng"}).engine_user == "eng"


def test_build_engine_registry_maps_render_docx_to_the_child_engine(tmp_path):
    from src.adapters.sandbox_engine.docx_render_engine import DocxRenderEngine
    out = tmp_path / "out"
    plan = _plan({"SANDBOX_OUTPUT_ROOT": str(out), "SANDBOX_WORK_ROOT": str(tmp_path / "w")})
    registry = build_engine_registry(plan)
    assert set(registry) == {SandboxOp.RENDER_DOCX}
    assert isinstance(registry[SandboxOp.RENDER_DOCX], DocxRenderEngine)
    assert out.is_dir() and (out.stat().st_mode & 0o077) == 0


def test_build_engine_registry_refuses_symlinked_output_root(tmp_path):
    target = tmp_path / "t"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link)
    plan = _plan({"SANDBOX_OUTPUT_ROOT": str(link), "SANDBOX_WORK_ROOT": str(tmp_path / "w")})
    with pytest.raises(SandboxStartupError):
        build_engine_registry(plan)


def test_output_root_not_inside_work_root_refused(tmp_path):
    w = tmp_path / "w"
    plan = _plan({"SANDBOX_WORK_ROOT": str(w), "SANDBOX_OUTPUT_ROOT": str(w / "out")})
    with pytest.raises(SandboxStartupError):
        build_engine_registry(plan)


def test_setup_module_imports_no_web_module():
    tree = ast.parse(pathlib.Path(sandbox_setup.__file__).read_text())
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mods.append(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
    for m in mods:
        for banned in ("web", "infrastructure", "agents", "handlers", "services"):
            assert banned not in m.split("."), m


def test_validate_startup_refuses_flag_disagreeing_with_k_service():
    with pytest.raises(SandboxStartupError):
        validate_startup(dict(_OK), on_cloud_run=True)
    with pytest.raises(SandboxStartupError):
        validate_startup({**_OK, "K_SERVICE": "svc"}, on_cloud_run=False)


def test_validate_startup_does_not_mutate_env():
    env = {**_OK, "K_SERVICE": "svc"}
    before = dict(env)
    validate_startup(env, on_cloud_run=True)
    assert env == before


def test_empty_url_on_cloud_run_with_callers_set_is_unconfigured_plan():
    plan = validate_startup({**_OK, "K_SERVICE": "svc", "SANDBOX_URL": ""}, on_cloud_run=True)
    assert plan.unconfigured is True


def test_existing_group_writable_output_root_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    out.chmod(0o770)
    plan = _plan({"SANDBOX_OUTPUT_ROOT": str(out), "SANDBOX_WORK_ROOT": str(tmp_path / "w")})
    with pytest.raises(SandboxStartupError):
        build_engine_registry(plan)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to create a foreign-owned directory")
def test_foreign_owned_output_root_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    os.chown(out, 12345, 12345)
    plan = _plan({"SANDBOX_OUTPUT_ROOT": str(out), "SANDBOX_WORK_ROOT": str(tmp_path / "w")})
    with pytest.raises(SandboxStartupError):
        build_engine_registry(plan)


def test_output_root_overlap_check_resolves_symlinked_work_root(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "wlink"
    os.symlink(real, link)
    plan = _plan({"SANDBOX_WORK_ROOT": str(link), "SANDBOX_OUTPUT_ROOT": str(real / "out")})
    with pytest.raises(SandboxStartupError):
        build_engine_registry(plan)


def test_validate_startup_local_without_callers_or_url_is_open():
    plan = validate_startup({}, on_cloud_run=False)
    assert plan.caller_sets == {} and plan.service_url == "" and plan.unconfigured is False


def test_validate_startup_local_with_callers_but_no_url_stays_unconfigured():
    assert _plan().unconfigured is True


def test_validate_startup_on_cloud_run_without_callers_is_still_refused():
    with pytest.raises(SandboxCallersError):
        validate_startup({"K_SERVICE": "svc"}, on_cloud_run=True)
