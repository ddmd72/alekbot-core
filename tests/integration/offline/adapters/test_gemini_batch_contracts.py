"""Integration tests for GeminiBatchAdapter contracts: the real adapter against a capturing stub of
`client.aio.batches`, validated by the named rules (same shape as the Gmail contract tests)."""
import pytest
from google.genai import types

from src.adapters.gemini_batch_adapter import GeminiBatchAdapter
from src.domain.batch_transcription import BatchRequest
from tests.contracts.adapter_contracts import (
    BATCH_FILE_URI_FROM_STORED_NAME,
    BATCH_KEY_ROUND_TRIP,
    BATCH_PER_RESPONSE_ERROR_SURFACED,
    BATCH_SCHEMA_WITH_JSON_MIME,
    BATCH_THINKING_LOW,
)

SCHEMA = {"type": "object", "properties": {"s": {"type": "array", "items": {"type": "string"}}}}


class BatchesCapturingStub:
    """Installs on adapter.client.aio.batches: records create() kwargs, serves a job on get()."""

    def __init__(self, inlined_responses=None):
        self.captured_kwargs: dict = {}
        self._responses = inlined_responses or []

    def install(self, adapter) -> "BatchesCapturingStub":
        async def create(**kwargs):
            self.captured_kwargs.update(kwargs)
            return types.BatchJob(name="batches/b1", state=types.JobState.JOB_STATE_PENDING)

        async def get(**kwargs):
            return types.BatchJob(name="batches/b1", state=types.JobState.JOB_STATE_SUCCEEDED,
                                  dest=types.BatchJobDestination(inlined_responses=self._responses))
        adapter.client.aio.batches.create = create
        adapter.client.aio.batches.get = get
        return self


def _requests():
    return [BatchRequest(key=f"w{i}", system_instruction="s", user_text="u", response_schema=SCHEMA,
                         file_name=f"files/f{i}", file_mime="audio/ogg") for i in range(2)]


@pytest.mark.asyncio
async def test_submit_satisfies_every_submit_rule():
    adapter = GeminiBatchAdapter(api_key="k", model="gemini-test")
    stub = BatchesCapturingStub().install(adapter)
    await adapter.submit(_requests(), display_name="d")
    for rule in (BATCH_SCHEMA_WITH_JSON_MIME, BATCH_THINKING_LOW, BATCH_KEY_ROUND_TRIP,
                 BATCH_FILE_URI_FROM_STORED_NAME):
        rule.validate("gemini_batch", stub.captured_kwargs)


@pytest.mark.asyncio
async def test_collect_satisfies_the_per_response_rule():
    adapter = GeminiBatchAdapter(api_key="k", model="gemini-test")
    ok = types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text="{}")]), finish_reason="STOP")])
    stub = BatchesCapturingStub([
        types.InlinedResponse(metadata={"key": "w0"}, response=ok),
        types.InlinedResponse(metadata={"key": "w1"}, error=types.JobError(code=3, message="x")),
    ]).install(adapter)
    ref = await adapter.submit(_requests(), display_name="d")
    results = await adapter.collect(ref)
    BATCH_PER_RESPONSE_ERROR_SURFACED.validate("gemini_batch", {"results": results, "failed_keys": ["w1"]})
    assert stub.captured_kwargs
