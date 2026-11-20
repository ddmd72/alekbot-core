"""DocumentReadPort (EXTERNAL_FILES_RFC §4.4): `read(request) -> ReadOutcome` carries every outcome."""


from src.domain.document_read import ReadOutcome, ReadRequest, ReadStatus
from src.ports.document_read_port import DocumentReadPort


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
