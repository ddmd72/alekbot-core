"""`parse.container_facts` result schema for OLE2 answers (owner decision 2026-11-05): `ole` is a status, carries
`members` and `vba_storage`, and the closed key set still refuses what is not part of the op."""
import json

import pytest

from src.domain.sandbox_contract import SandboxOp
from src.domain.sandbox_parse_result import ParseResultError, decode_parse_result, encode_parse_result

OP = SandboxOp.PARSE_CONTAINER_FACTS


def _raw(obj) -> bytes:
    return json.dumps(obj).encode()


def test_an_ole_answer_round_trips():
    obj = {"status": "ole", "members": 4, "vba_storage": True}
    assert decode_parse_result(encode_parse_result(decode_parse_result(_raw(obj), OP)), OP) == obj


@pytest.mark.parametrize("obj", [
    {"status": "ole", "members": 4},                          # an ole answer without its fact
    {"status": "ole", "vba_storage": True},                   # ... without its member count
    {"status": "ole", "members": 4, "vba_storage": "yes"},
    {"status": "ole", "members": 4, "vba_storage": True, "vba_project": True, "text": "x"},
    {"status": "zip", "members": 3, "vba_storage": True},     # a zip answer without its four facts
])
def test_an_incomplete_or_foreign_ole_answer_is_refused(obj):
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw(obj), OP)
