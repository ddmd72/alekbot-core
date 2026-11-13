"""The fact profile step bills its label embeddings through the graph's quota service (decisions/billing_embeddings.md).

The step is built by `build_fact_profile_service` from a container that holds no quota service, so the quota is an
explicit keyword; a quota left out would silently leave the step's embeddings unbilled."""
import ast
from pathlib import Path
from types import SimpleNamespace

from src.composition.fact_profile_setup import build_fact_profile_service

ROOT = Path(__file__).resolve().parents[3]


def test_the_builder_hands_its_quota_to_the_step():
    container = SimpleNamespace(repository=object(), fact_profiler=object(), embedding_service=object(),
                                confirmed_people=None, profile_answers=None)
    quota = object()
    assert build_fact_profile_service(container, max_per_run=None, quota=quota)._quota is quota   # type: ignore[arg-type]


def test_the_graph_passes_its_quota_service_to_the_builder():
    tree = ast.parse((ROOT / "src/composition/application_graph.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "build_fact_profile_service"]
    assert len(calls) == 1
    quota = {k.arg: k.value for k in calls[0].keywords}.get("quota")
    assert isinstance(quota, ast.Name) and quota.id == "quota_service"
