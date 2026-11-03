"""`build_document_service` uses the parse adapter its caller gives, as parser, splitter and the reader's splitter,
and builds no parser child itself (UNTRUSTED_CONTENT_SANDBOX_RFC §5 O14)."""
from pathlib import Path

from src.composition import document_service_setup
from src.composition.document_service_setup import build_document_service

PARSER = object()


def _build():
    return build_document_service(context_builder=object(), users=object(), assembly_service=object(), quota=None,
                                  transcriber=None, transcript_cache=None, parser=PARSER)


def test_the_given_object_is_parser_splitter_and_the_readers_splitter():
    service = _build()
    assert service._parser is PARSER and service._splitter is PARSER
    assert service._reader._splitter is PARSER


def test_the_module_has_no_parser_child_constant_or_import():
    assert not hasattr(document_service_setup, "MAIN_PARSER_MAX_CHILDREN")
    assert "subprocess_content_extractor" not in Path(document_service_setup.__file__).read_text()
