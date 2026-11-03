"""DocumentReadPort (EXTERNAL_FILES_RFC §4.4): one abstract async method, `read(request) -> ReadOutcome`."""
import inspect
from abc import ABC

import pytest

from src.domain.document_read import ReadOutcome, ReadRequest, ReadStatus
from src.ports.document_read_port import DocumentReadPort


def test_the_port_is_abstract_with_one_method():
    assert issubclass(DocumentReadPort, ABC)
    with pytest.raises(TypeError):
        DocumentReadPort()
    assert DocumentReadPort.__abstractmethods__ == frozenset({"read"})


def test_read_takes_a_request_and_returns_an_outcome():
    assert inspect.iscoroutinefunction(DocumentReadPort.read)
    sig = inspect.signature(DocumentReadPort.read)
    assert list(sig.parameters) == ["self", "request"]
    assert sig.parameters["request"].annotation in (ReadRequest, "ReadRequest")
    assert sig.return_annotation in (ReadOutcome, "ReadOutcome")


async def test_a_still_working_adapter_answers_through_the_port():
    """STILL_WORKING is reserved for a remote adapter (D1): the port carries it like any outcome."""
    class _Remote(DocumentReadPort):
        async def read(self, request: ReadRequest) -> ReadOutcome:
            return ReadOutcome(status=ReadStatus.STILL_WORKING, detail=request.request_id)

    request = ReadRequest(content=b"x", name="scan_0012.pdf", declared_mime="application/pdf", store="chat",
                          caller="open", native_formats=(), user_id="ann", account_id="ann", deadline_at=0.0,
                          request_id="r1")
    outcome = await _Remote().read(request)
    assert outcome.status is ReadStatus.STILL_WORKING and outcome.detail == "r1"
