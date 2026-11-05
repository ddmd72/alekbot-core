"""S5 card B: the light family's tunnel engines and the indexer's `media` prefix (S5 design §2.5, §2.7)."""
import os
import sys
import types

import pytest

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.composition.sandbox_setup import LIGHT_GROUP_PREFIXES, build_tunnel_registry, validate_startup
from src.domain.sandbox_contract import SandboxOp
from src.domain.sandbox_tunnel import TUNNEL_OPS

_MEDIA_ENGINE = "src.adapters.sandbox_engine.media_engine"


class RecordingMediaEngine:
    """Stands in for card A's `MediaEngine(launcher, op=..., output_root=...)`."""

    def __init__(self, launcher, *, op, output_root):
        self.launcher, self.op, self.output_root = launcher, op, output_root


@pytest.fixture
def media_engine(monkeypatch):
    module = types.ModuleType(_MEDIA_ENGINE)
    module.MediaEngine = RecordingMediaEngine
    monkeypatch.setitem(sys.modules, _MEDIA_ENGINE, module)


def _plan(tmp_path, family):
    env = {"SANDBOX_WORK_ROOT": str(tmp_path / "w"), "SANDBOX_OUTPUT_ROOT": str(tmp_path / "o")}
    env.update({"SANDBOX_SERVICE": "light", "SANDBOX_CALLERS_INDEXER": "idx@example.com"} if family == "light"
               else {"SANDBOX_CALLERS_RENDER": "r@example.com"})
    return validate_startup(env, on_cloud_run=False)


def test_light_maps_each_tunnel_op_to_a_media_engine_on_one_launcher(tmp_path, media_engine):
    registry = build_tunnel_registry(_plan(tmp_path, "light"))
    assert set(registry) == set(TUNNEL_OPS) == {SandboxOp.MEDIA_PROBE, SandboxOp.MEDIA_AUDIO_WINDOWS}
    assert {op: engine.op for op, engine in registry.items()} == {op: op for op in registry}
    launchers = {id(engine.launcher) for engine in registry.values()}
    assert len(launchers) == 1 and isinstance(next(iter(registry.values())).launcher, ChildLauncher)
    assert {engine.output_root for engine in registry.values()} == {os.path.realpath(tmp_path / "o")}


def test_render_builds_no_tunnel_engine(tmp_path):
    assert build_tunnel_registry(_plan(tmp_path, "render")) == {}


def test_the_indexer_gains_media_and_so_does_the_main_service_since_s5b():
    assert LIGHT_GROUP_PREFIXES["INDEXER"] == frozenset({"parse", "media"})
    assert LIGHT_GROUP_PREFIXES["MAIN"] == frozenset({"parse", "media"})
