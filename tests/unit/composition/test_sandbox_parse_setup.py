"""build_sandbox_parser: one object serves both ports, an empty URL is refused with an ERROR, nothing reads the
environment (no knob)."""
import ast
import logging
from pathlib import Path

import pytest

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.composition import sandbox_parse_setup
from src.composition.sandbox_parse_setup import build_sandbox_parser
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.pdf_splitter_port import PdfSplitterPort


def test_one_object_serves_both_ports():
    parser = build_sandbox_parser("https://light.example/")
    assert isinstance(parser, SandboxContentExtractor)
    assert isinstance(parser, ContentExtractorPort) and isinstance(parser, PdfSplitterPort)


@pytest.mark.parametrize("url", ["", "   ", None])
def test_an_empty_url_is_refused_with_an_error_line(url, caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError):
        build_sandbox_parser(url)  # type: ignore[arg-type]
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_the_setup_reads_no_environment():
    tree = ast.parse(Path(sandbox_parse_setup.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree)
                                                                          if isinstance(n, ast.Attribute)}
    assert not names & {"environ", "getenv", "os"}
