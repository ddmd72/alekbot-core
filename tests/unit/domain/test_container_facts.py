"""`parse.container_facts` (EXTERNAL_FILES D13, owner decision O2): the op's closed result schema in
`sandbox_parse_result`, and the `ContainerFacts` value the main side builds from it."""
import json

import pytest

from src.domain.container_facts import ContainerFacts, ContainerStatus, container_facts_from_result
from src.domain.sandbox_contract import SandboxOp
from src.domain.sandbox_parse_result import ParseResultError, decode_parse_result, encode_parse_result

OP = SandboxOp.PARSE_CONTAINER_FACTS
ZIP = {"status": "zip", "members": 12, "vba_project": True, "macro_content_type": False, "odf_macros": False}


def _raw(obj) -> bytes:
    return json.dumps(obj).encode()


def test_the_op_has_its_wire_name():
    assert OP.value == "parse.container_facts"


@pytest.mark.parametrize("obj", [
    ZIP, {"status": "not_zip"}, {"status": "too_large", "members": 10_001, "detail": "10001 members"},
    {"status": "error", "detail": "BadZipFile"},
])
def test_each_status_round_trips(obj):
    assert decode_parse_result(encode_parse_result(decode_parse_result(_raw(obj), OP)), OP) == obj


@pytest.mark.parametrize("obj", [
    {"status": "text"},                                   # a parser status, not this op's
    {**ZIP, "text": "x"},                                 # a key of the other parse ops
    {"status": "zip", "members": 3},                      # a zip answer without every fact
    {**ZIP, "members": -1},
    {**ZIP, "vba_project": "yes"},
    {**ZIP, "user_id": "u1"},                             # no identity, ever
])
def test_anything_outside_the_closed_schema_is_refused(obj):
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw(obj), OP)


def test_the_other_parse_ops_do_not_accept_container_keys():
    with pytest.raises(ParseResultError):
        decode_parse_result(_raw({"status": "text", "text": "t", "vba_project": True}), SandboxOp.PARSE_EXTRACT)


def test_facts_from_a_result():
    facts = container_facts_from_result(decode_parse_result(_raw(ZIP), OP))
    assert facts == ContainerFacts(status=ContainerStatus.ZIP, members=12, vba_project=True)
    assert facts.checked and facts.carries_macros


@pytest.mark.parametrize("facts, checked, macros", [
    (ContainerFacts(ContainerStatus.ZIP, members=4), True, False),
    (ContainerFacts(ContainerStatus.ZIP, macro_content_type=True), True, True),
    (ContainerFacts(ContainerStatus.ZIP, odf_macros=True), True, True),
    (ContainerFacts(ContainerStatus.NOT_ZIP), True, False),
    (ContainerFacts(ContainerStatus.TOO_LARGE, members=10_001, vba_project=True), False, False),
    (ContainerFacts(ContainerStatus.ERROR), False, False),
])
def test_checked_and_carries_macros(facts, checked, macros):
    assert (facts.checked, facts.carries_macros) == (checked, macros)
