"""The service container passes the values production gives it (P9 T5b, round 3: the wiring mutants no earlier test killed).

Same two graphs as `test_application_graph_values.py`: `prod_graph` (the deploy's sealed environment) and `variant_graph`
(the SDK-fallback environment variables absent, so a key the container dropped cannot be rescued by the SDK reading its own
variable). Each check reads the attribute that holds the value in the object production uses.
"""
from pathlib import Path

import pytest

from tests.support.prod_graph import ProdGraph, prod_graph  # noqa: F401  (the fixture)
from tests.unit.composition.test_application_graph_values import variant_graph  # noqa: F401  (the fixture)

pytestmark = pytest.mark.slow

SYSTEM_SKILL_FILE = Path(__file__).resolve().parents[3] / "src/skills/smart/domain-competency-research/references/final-manifest.md"


def test_the_claude_and_openai_adapters_hold_the_configured_keys(variant_graph):
    app, config = variant_graph

    assert app.container.claude_service.client.api_key == config["ANTHROPIC_API_KEY"]
    assert app.container.openai_service.client.api_key == config["OPENAI_API_KEY"]


async def test_the_prompt_components_and_the_sessions_live_in_the_environments_collections(prod_graph):
    app = prod_graph.app
    env_config = app.agent_factory.env_config

    assert app.container.prompt_component_repo.collection.name == f"{env_config.firestore_collection_prefix}prompt_components"
    assert app.container.prompt_component_repo.collection.name != "prompt_components"
    assert app.container.session_store.collection_name == env_config.sessions_collection


async def test_a_system_skill_file_is_read_from_the_bundle_through_open_file(prod_graph):
    resolver = prod_graph.app.container.file_conversion_service._skill_files

    text = await resolver.read("u1", "skill:domain-competency-research/references/final-manifest.md")

    assert text == SYSTEM_SKILL_FILE.read_text(encoding="utf-8")
