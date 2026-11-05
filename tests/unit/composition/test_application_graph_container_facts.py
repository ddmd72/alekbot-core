"""D13's container check is wired in production (EXTERNAL_FILES U4b, owner decision O2): the file conversion service's
container-facts reader is the graph's one sandbox parse adapter, the same object its document service parses through,
so the macro check runs in the light service and a missing reader (every send of a ZIP-family attachment refused as
unchecked) cannot ship silently."""
import pytest

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.ports.container_facts_port import ContainerFactsPort
from tests.support.prod_graph import prod_graph  # noqa: F401  (fixture)

pytestmark = pytest.mark.slow


async def test_the_container_reader_is_the_sandbox_parse_adapter(prod_graph):
    service = prod_graph.app.container.file_conversion_service
    assert isinstance(service._containers, SandboxContentExtractor)
    assert isinstance(service._containers, ContainerFactsPort)
    assert service._containers is service._documents._parser
