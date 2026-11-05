"""Service family selector of the sandbox (plan P1): `render` (default) or `light`."""
import pytest

from src.composition.sandbox_callers import SandboxCallersError
from src.composition.sandbox_setup import (
    GROUP_PREFIXES, LIGHT_GROUP_PREFIXES, SandboxStartupError, StartupPlan, build_engine_registry, validate_startup,
)
from src.domain.sandbox_contract import SandboxOp


def _light(extra=None, on_cloud_run=False):
    env = {"SANDBOX_SERVICE": "light", "SANDBOX_CALLERS_MAIN": "main@example.com", **(extra or {})}
    if on_cloud_run:
        env.setdefault("K_SERVICE", "svc")
    return validate_startup(env, on_cloud_run=on_cloud_run)


def test_default_family_is_render_and_unchanged():
    plan = validate_startup({"SANDBOX_CALLERS_RENDER": "r@example.com"}, on_cloud_run=False)
    assert plan.family == "render" and plan.group_prefixes == GROUP_PREFIXES == {"RENDER": frozenset({"render"})}


def test_the_family_is_the_last_plan_field_and_defaults_to_render():
    import dataclasses

    fields = dataclasses.fields(StartupPlan)
    assert fields[-1].name == "family" and fields[-1].default == "render"


def test_light_prefixes_give_main_and_indexer_the_parse_ops():
    # S5 (owner decision): the INDEXER group gains `media` (drive transcripts); S5b: MAIN gains `media` (edit-video probe).
    assert LIGHT_GROUP_PREFIXES == {"MAIN": frozenset({"parse", "media"}), "INDEXER": frozenset({"parse", "media"})}


def test_light_accepts_one_or_both_groups():
    assert _light().group_prefixes == LIGHT_GROUP_PREFIXES and _light().family == "light"
    both = _light({"SANDBOX_CALLERS_INDEXER": "idx@example.com"})
    assert set(both.caller_sets) == {"MAIN", "INDEXER"}


def test_light_refuses_the_render_group():
    with pytest.raises(SandboxStartupError):
        _light({"SANDBOX_CALLERS_RENDER": "r@example.com"})
    with pytest.raises(SandboxStartupError):
        validate_startup({"SANDBOX_SERVICE": "light", "SANDBOX_CALLERS_RENDER": "r@example.com"}, on_cloud_run=False)


def test_light_refuses_an_unknown_group_and_no_known_group():
    with pytest.raises(SandboxStartupError):
        _light({"SANDBOX_CALLERS_MYSTERY": "z@example.com"})
    with pytest.raises(SandboxStartupError):
        validate_startup({"SANDBOX_SERVICE": "light", "SANDBOX_URL": "https://svc.example.com"}, on_cloud_run=False)


def test_render_refuses_the_light_groups():
    with pytest.raises(SandboxStartupError):
        validate_startup({"SANDBOX_CALLERS_MAIN": "m@example.com"}, on_cloud_run=False)


@pytest.mark.parametrize("family", ["", "heavy", "LIGHT", "render,light"])
def test_unknown_family_is_refused(family):
    with pytest.raises(SandboxStartupError):
        validate_startup({"SANDBOX_SERVICE": family, "SANDBOX_CALLERS_RENDER": "r@example.com"}, on_cloud_run=False)


def test_light_on_cloud_run_without_callers_is_still_refused_and_without_url_is_unconfigured():
    with pytest.raises(SandboxCallersError):
        validate_startup({"SANDBOX_SERVICE": "light", "K_SERVICE": "svc"}, on_cloud_run=True)
    plan = _light(on_cloud_run=True)
    assert plan.unconfigured is True and plan.engine_user == "sandboxengine"


def test_light_registry_maps_the_four_parse_ops_and_no_render_op(tmp_path):
    """EXTERNAL_FILES U4b (D13, owner decision O2): `parse.container_facts` is the fourth parse op."""
    from src.adapters.sandbox_engine.parse_engine import ParseEngine

    plan = _light({"SANDBOX_WORK_ROOT": str(tmp_path / "w"), "SANDBOX_OUTPUT_ROOT": str(tmp_path / "o")})
    registry = build_engine_registry(plan)
    assert set(registry) == {SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS,
                             SandboxOp.PARSE_CONTAINER_FACTS}
    assert all(isinstance(engine, ParseEngine) for engine in registry.values())
    assert {op: engine._op for op, engine in registry.items()} == {op: op for op in registry}


def test_render_registry_holds_the_render_ops_and_no_parse_op(tmp_path):
    """The family selector leaves the render registry to the render ops (S1 docx + S2 pdf/png, RFC §7 S2)."""
    plan = validate_startup({"SANDBOX_CALLERS_RENDER": "r@example.com", "SANDBOX_WORK_ROOT": str(tmp_path / "w"),
                             "SANDBOX_OUTPUT_ROOT": str(tmp_path / "o")}, on_cloud_run=False)
    assert set(build_engine_registry(plan)) == {SandboxOp.RENDER_DOCX, SandboxOp.RENDER_PDF, SandboxOp.RENDER_PNG}
