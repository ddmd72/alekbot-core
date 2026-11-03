"""The main service parses only through the sandbox light service (UNTRUSTED_CONTENT_SANDBOX_RFC §4.10, §5 O14):
`build_application_graph` builds the parse adapter right after the digest key, so an empty URL refuses the start
before any repository exists (locally too), and the document service it builds runs no in-process parser child."""
from typing import Any

import pytest

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.composition.application_graph import build_application_graph
from tests.support import prod_env
from tests.support import prod_graph as prod_graph_module
from tests.support.prod_graph import prod_graph  # noqa: F401  (fixture)


class UntouchableDb:
    """A Firestore client the graph must not reach: any attribute access fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"build_application_graph touched the database ({name}) before refusing to start")


@pytest.mark.parametrize("value", [None, "", "  "], ids=["unset", "empty", "blank"])
def test_the_graph_refuses_to_build_without_the_light_service_url(monkeypatch, caplog, value):
    config, _ = prod_env.prod_settings(monkeypatch)
    config["SANDBOX_PARSE_URL"] = value

    with pytest.raises(ValueError, match="light service URL"):
        build_application_graph(config, UntouchableDb())

    assert any(r.levelname == "ERROR" and "light service URL" in r.getMessage() for r in caplog.records)


async def test_the_document_service_parses_through_the_sandbox(prod_graph):
    documents = prod_graph.app.container.file_conversion_service._documents
    assert isinstance(documents._parser, SandboxContentExtractor)
    assert documents._splitter is documents._parser
    assert documents._reader._splitter is documents._parser


async def test_no_in_process_parser_child_is_built(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the main service built an in-process parser child")

    monkeypatch.setattr(SubprocessContentExtractor, "__init__", fail)
    graph = await prod_graph_module.build(monkeypatch)
    await graph.app.shutdown()
