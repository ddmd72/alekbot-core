"""Wire tests for GeminiBatchAdapter: the SDK client's aio.files / aio.batches are the mocked boundary, never the
port (docs/how_to/ADAPTER_WIRE_TESTING.md). Fixtures are hand-built from google.genai.types; no live call."""
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types

from src.adapters.gemini_batch_adapter import GeminiBatchAdapter
from src.domain.batch_transcription import BatchRef, BatchRequest, BatchStatus, FileRef
from src.domain.exceptions import LLMAccountError, LLMClientError, LLMRateLimitError, LLMUnavailableError
from tests.contracts.adapter_contracts import (
    BATCH_FILE_URI_FROM_STORED_NAME,
    BATCH_KEY_ROUND_TRIP,
    BATCH_PER_RESPONSE_ERROR_SURFACED,
    BATCH_SCHEMA_WITH_JSON_MIME,
    BATCH_THINKING_LOW,
    BATCH_THOUGHTS_IN_OUTPUT_LEG,
)

MODEL = "gemini-test-model"
SCHEMA = {"type": "object", "properties": {"segments": {"type": "array", "items": {"type": "string"}}},
          "required": ["segments"]}


def _adapter() -> GeminiBatchAdapter:
    return GeminiBatchAdapter(api_key="test-key", model=MODEL)


def _req(key="w0", file_name="files/abc", **kw) -> BatchRequest:
    return BatchRequest(key=key, system_instruction="sys", user_text="go", response_schema=SCHEMA,
                        file_name=file_name, file_mime="audio/ogg", max_output_tokens=1000, **kw)


def _install_create(adapter, captured):
    async def create(**kwargs):
        captured.update(kwargs)
        return types.BatchJob(name="batches/b1", state=types.JobState.JOB_STATE_PENDING)
    adapter.client.aio.batches.create = create


async def _submit(requests, display_name="user-123:item-9"):
    adapter, captured = _adapter(), {}
    _install_create(adapter, captured)
    ref = await adapter.submit(requests, display_name=display_name)
    return ref, captured


def _response(text="[]", finish="STOP", usage=None, feedback=None):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]),
                                    finish_reason=finish)] if finish else [],
        usage_metadata=usage, prompt_feedback=feedback)


def _usage(audio=4501, text=108, cand=1269, thoughts=None):
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=audio + text, candidates_token_count=cand, thoughts_token_count=thoughts,
        total_token_count=audio + text + cand + (thoughts or 0),
        prompt_tokens_details=[types.ModalityTokenCount(modality=types.MediaModality.AUDIO, token_count=audio),
                               types.ModalityTokenCount(modality=types.MediaModality.TEXT, token_count=text)])


def _job(responses, state=types.JobState.JOB_STATE_SUCCEEDED):
    return types.BatchJob(name="batches/b1", state=state,
                          dest=types.BatchJobDestination(inlined_responses=responses))


def _inlined(key="w0", **kw):
    return types.InlinedResponse(response=_response(**kw), metadata={"key": key})


async def _collect(job, keys=("w0",)):
    adapter = _adapter()

    async def get(**kwargs):
        return job
    adapter.client.aio.batches.get = get
    return await adapter.collect(BatchRef(name="batches/b1", keys=tuple(keys)))


def _sdk_error(cls, code, msg="boom"):
    return cls(code, {"error": {"code": code, "message": msg, "status": "X"}}, None)


@pytest.mark.asyncio
async def test_upload_sends_mime_and_returns_expiry():
    adapter, captured = _adapter(), {}
    expiry = datetime.now(timezone.utc) + timedelta(hours=48)

    async def upload(**kwargs):
        captured.update(kwargs)
        return types.File(name="files/abc", uri="https://example.invalid/v1beta/files/abc", expiration_time=expiry)
    adapter.client.aio.files.upload = upload
    ref = await adapter.upload_audio("/tmp/window_0.opus", "audio/ogg")
    assert captured["file"] == "/tmp/window_0.opus"
    assert captured["config"].mime_type == "audio/ogg"
    assert ref.name == "files/abc" and ref.expires_at == expiry
    assert "window_0" not in (captured["config"].display_name or "")


@pytest.mark.asyncio
async def test_submit_inline_response_schema_not_json_schema():
    _, captured = await _submit([_req()])
    cfg = captured["src"][0].config
    assert isinstance(cfg.response_schema, types.Schema)
    assert cfg.response_json_schema is None
    assert captured["model"] == MODEL


@pytest.mark.asyncio
async def test_json_mime_type_with_schema():
    _, captured = await _submit([_req()])
    BATCH_SCHEMA_WITH_JSON_MIME.validate("gemini_batch", captured)
    assert captured["src"][0].config.response_mime_type == "application/json"


@pytest.mark.asyncio
async def test_thinking_low_on_every_request():
    _, captured = await _submit([_req("w0"), _req("w1"), BatchRequest(key="t", system_instruction="s", user_text="u")])
    BATCH_THINKING_LOW.validate("gemini_batch", captured)
    BATCH_KEY_ROUND_TRIP.validate("gemini_batch", captured)
    assert [r.metadata for r in captured["src"]] == [{"key": "w0"}, {"key": "w1"}, {"key": "t"}]


@pytest.mark.asyncio
async def test_submit_builds_file_uri_from_stored_name():
    ref, captured = await _submit([_req(file_name="files/xyz")])
    BATCH_FILE_URI_FROM_STORED_NAME.validate("gemini_batch", captured)
    part = captured["src"][0].contents[0].parts[0]
    assert part.file_data.file_uri.endswith("/files/xyz") and part.file_data.mime_type == "audio/ogg"
    assert ref == BatchRef(name="batches/b1", keys=("w0",))


@pytest.mark.asyncio
async def test_display_name_has_no_user_id():
    _, captured = await _submit([_req()], display_name="user-123:item-9")
    shown = captured["config"].display_name
    assert shown and "user-123" not in shown and "item-9" not in shown


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [
    (types.JobState.JOB_STATE_UNSPECIFIED, BatchStatus.PENDING),
    (types.JobState.JOB_STATE_QUEUED, BatchStatus.PENDING),
    (types.JobState.JOB_STATE_PENDING, BatchStatus.PENDING),
    (types.JobState.JOB_STATE_RUNNING, BatchStatus.RUNNING),
    (types.JobState.JOB_STATE_UPDATING, BatchStatus.RUNNING),
    (types.JobState.JOB_STATE_PAUSED, BatchStatus.RUNNING),
    (types.JobState.JOB_STATE_CANCELLING, BatchStatus.RUNNING),
    (types.JobState.JOB_STATE_SUCCEEDED, BatchStatus.SUCCEEDED),
    (types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED, BatchStatus.SUCCEEDED),
    (types.JobState.JOB_STATE_FAILED, BatchStatus.FAILED),
    (types.JobState.JOB_STATE_CANCELLED, BatchStatus.CANCELLED),
    (types.JobState.JOB_STATE_EXPIRED, BatchStatus.EXPIRED),
])
async def test_poll_maps_every_state(state, expected):
    adapter, seen = _adapter(), {}

    async def get(**kwargs):
        seen.update(kwargs)
        return types.BatchJob(name="batches/b1", state=state)
    adapter.client.aio.batches.get = get
    assert await adapter.poll(BatchRef(name="batches/b1")) == expected
    assert seen["name"] == "batches/b1"


@pytest.mark.asyncio
async def test_collect_checks_each_response_error():
    job = _job([_inlined("w0", text='{"ok":1}'),
                types.InlinedResponse(metadata={"key": "w1"}, error=types.JobError(code=3, message="bad input"))])
    out = await _collect(job, ("w0", "w1"))
    assert out["w0"].text == '{"ok":1}' and out["w0"].error is None
    assert out["w1"].text is None and out["w1"].error and not out["w1"].refused
    assert out["w1"].code == 3 and not out["w1"].transient
    BATCH_PER_RESPONSE_ERROR_SURFACED.validate("gemini_batch", {"results": out, "failed_keys": ["w1"]})
    with pytest.raises(AssertionError):  # failing everything must not pass the rule
        BATCH_PER_RESPONSE_ERROR_SURFACED.validate("gemini_batch", {"results": out, "failed_keys": ["w0", "w1"]})


@pytest.mark.asyncio
async def test_collect_key_mismatch_is_error():
    # Answer carries an unknown key: never matched by position, the expected key becomes an error result.
    job = _job([_inlined("zzz")])
    out = await _collect(job, ("w0",))
    assert out["w0"].text is None and "key" in out["w0"].error
    assert "zzz" not in out


@pytest.mark.asyncio
async def test_collect_missing_answer_is_error_and_order_is_not_trusted():
    job = _job([_inlined("w1", text="B")])
    out = await _collect(job, ("w0", "w1"))
    assert out["w1"].text == "B"
    assert out["w0"].error and out["w0"].text is None


@pytest.mark.asyncio
async def test_usage_by_modality_parse():
    out = await _collect(_job([_inlined(usage=_usage(audio=4501, text=108, cand=1269))]))
    u = out["w0"].usage
    assert (u.model, u.audio_input_tokens, u.text_input_tokens, u.output_tokens, u.thought_tokens) == \
        (MODEL, 4501, 108, 1269, 0)
    assert out["w0"].finish_reason == "STOP"


@pytest.mark.asyncio
async def test_thoughts_billed_as_output():
    out = await _collect(_job([_inlined(usage=_usage(cand=1000, thoughts=500))]))
    BATCH_THOUGHTS_IN_OUTPUT_LEG.validate("gemini_batch", {"usage": out["w0"].usage, "candidates": 1000, "thoughts": 500})
    assert out["w0"].usage.thought_tokens == 500


@pytest.mark.asyncio
async def test_usage_without_modality_split_counts_as_text():
    usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=77, candidates_token_count=5)
    out = await _collect(_job([_inlined(usage=usage)]))
    assert out["w0"].usage.text_input_tokens == 77 and out["w0"].usage.audio_input_tokens == 0


@pytest.mark.asyncio
async def test_refused_request_is_a_result_error():
    blocked = _inlined("w0", text="", finish="PROHIBITED_CONTENT")
    feedback = types.GenerateContentResponsePromptFeedback(block_reason=types.BlockedReason.SAFETY)
    prompt_blocked = types.InlinedResponse(metadata={"key": "w1"}, response=_response(finish=None, feedback=feedback))
    out = await _collect(_job([blocked, prompt_blocked]), ("w0", "w1"))
    for k in ("w0", "w1"):
        assert out[k].refused and out[k].error and out[k].text is None


@pytest.mark.asyncio
async def test_empty_text_with_normal_finish_is_a_plain_error():
    out = await _collect(_job([_inlined("w0", text="", finish="STOP")]))
    assert out["w0"].error and not out["w0"].refused


@pytest.mark.asyncio
async def test_delete_file():
    adapter, seen = _adapter(), []

    async def delete(**kwargs):
        seen.append(kwargs)
    adapter.client.aio.files.delete = delete
    ref = FileRef(name="files/abc", uri="u", expires_at=datetime.now(timezone.utc))
    await adapter.delete_file(ref)
    assert seen == [{"name": "files/abc"}]


@pytest.mark.asyncio
async def test_delete_file_already_gone_is_quiet():
    adapter = _adapter()

    async def delete(**kwargs):
        raise _sdk_error(genai_errors.ClientError, 404, "gone")
    adapter.client.aio.files.delete = delete
    await adapter.delete_file(FileRef(name="files/abc", uri="u", expires_at=datetime.now(timezone.utc)))


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", [
    (_sdk_error(genai_errors.ClientError, 429), LLMRateLimitError),
    (_sdk_error(genai_errors.ServerError, 503), LLMUnavailableError),
    (_sdk_error(genai_errors.ServerError, 500), LLMUnavailableError),
    (_sdk_error(genai_errors.ServerError, 504), LLMUnavailableError),
    (httpx.ConnectError("net"), LLMUnavailableError),
    (httpx.ReadTimeout("slow"), LLMUnavailableError),
    (asyncio.TimeoutError(), LLMUnavailableError),
    (_sdk_error(genai_errors.ClientError, 403, "permission denied"), LLMAccountError),
    (_sdk_error(genai_errors.ClientError, 401, "bad key"), LLMAccountError),
    (_sdk_error(genai_errors.ClientError, 400, "Billing account is not active for this project"), LLMAccountError),
    (_sdk_error(genai_errors.ClientError, 400, "You exceeded your current quota, check billing"), LLMAccountError),
])
async def test_rate_limit_and_5xx_are_outages(exc, expected):
    for call in ("poll", "submit", "upload"):
        adapter = _adapter()

        async def boom(**kwargs):
            raise exc
        adapter.client.aio.batches.get = boom
        adapter.client.aio.batches.create = boom
        adapter.client.aio.files.upload = boom
        with pytest.raises(expected):
            if call == "poll":
                await adapter.poll(BatchRef(name="b"))
            elif call == "submit":
                await adapter.submit([_req()], display_name="x")
            else:
                await adapter.upload_audio("/p", "audio/ogg")


@pytest.mark.asyncio
async def test_account_error_is_an_outage():
    adapter = _adapter()

    async def boom(**kwargs):
        raise _sdk_error(genai_errors.ClientError, 403, "API key not valid")
    adapter.client.aio.batches.get = boom
    with pytest.raises(LLMAccountError) as ei:
        await adapter.poll(BatchRef(name="b"))
    assert ei.value.http_status == 403


@pytest.mark.asyncio
async def test_plain_400_is_a_client_error_not_an_outage():
    adapter = _adapter()

    async def boom(**kwargs):
        raise _sdk_error(genai_errors.ClientError, 400, "request payload malformed")
    adapter.client.aio.batches.create = boom
    with pytest.raises(LLMClientError) as ei:
        await adapter.submit([_req()], display_name="x")
    assert not isinstance(ei.value, (LLMAccountError, LLMUnavailableError, LLMRateLimitError))


@pytest.mark.asyncio
async def test_per_response_transient_code_is_a_result_and_siblings_survive():
    job = _job([types.InlinedResponse(metadata={"key": "w0"}, error=types.JobError(code=8, message="exhausted")),
                _inlined("w1", text="B")])
    out = await _collect(job, ("w0", "w1"))
    assert out["w0"].text is None and out["w0"].error and out["w0"].code == 8 and out["w0"].transient
    assert out["w1"].text == "B"


@pytest.mark.asyncio
async def test_per_response_account_code_is_a_result_not_a_raise():
    out = await _collect(_job([types.InlinedResponse(metadata={"key": "w0"},
                                                     error=types.JobError(code=7, message="denied"))]))
    assert out["w0"].code == 7 and not out["w0"].transient and out["w0"].error


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    aiohttp.ServerDisconnectedError(),
    aiohttp.ClientOSError(104, "reset"),
    aiohttp.ClientConnectionError("down"),
])
async def test_aiohttp_transport_errors_are_outages(exc):
    for name in ("poll", "collect", "delete"):
        adapter = _adapter()

        async def boom(**kwargs):
            raise exc
        adapter.client.aio.batches.get = boom
        adapter.client.aio.files.delete = boom
        with pytest.raises(LLMUnavailableError):
            if name == "poll":
                await adapter.poll(BatchRef(name="b"))
            elif name == "collect":
                await adapter.collect(BatchRef(name="b", keys=("w0",)))
            else:
                await adapter.delete_file(FileRef(name="files/a", uri="u", expires_at=datetime.now(timezone.utc)))


def _info_error(code, status, reason, msg="invalid"):
    body = {"error": {"code": code, "status": status, "message": msg,
                      "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]}}
    return genai_errors.ClientError(code, body, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    _info_error(400, "INVALID_ARGUMENT", "API_KEY_INVALID", "request is bad"),
    _info_error(400, "FAILED_PRECONDITION", "BILLING_DISABLED", "request is bad"),
    _info_error(400, "FAILED_PRECONDITION", "SOMETHING_ELSE", "User location is not supported"),
    _info_error(400, "UNAUTHENTICATED", "ACCESS_TOKEN_EXPIRED", "request is bad"),
])
async def test_account_errors_by_structured_fields(exc):
    adapter = _adapter()

    async def boom(**kwargs):
        raise exc
    adapter.client.aio.batches.get = boom
    with pytest.raises(LLMAccountError):
        await adapter.poll(BatchRef(name="b"))


@pytest.mark.asyncio
async def test_unrelated_reason_with_plain_400_is_a_client_error():
    adapter = _adapter()

    async def boom(**kwargs):
        raise _info_error(400, "INVALID_ARGUMENT", "FIELD_VIOLATION", "bad field")
    adapter.client.aio.batches.create = boom
    with pytest.raises(LLMClientError) as ei:
        await adapter.submit([_req()], display_name="x")
    assert not isinstance(ei.value, LLMAccountError)


@pytest.mark.asyncio
async def test_delete_file_403_is_already_gone():
    adapter = _adapter()

    async def delete(**kwargs):
        raise _sdk_error(genai_errors.ClientError, 403, "permission denied or not found")
    adapter.client.aio.files.delete = delete
    await adapter.delete_file(FileRef(name="files/abc", uri="u", expires_at=datetime.now(timezone.utc)))


@pytest.mark.asyncio
async def test_delete_file_outage_still_raises():
    adapter = _adapter()

    async def delete(**kwargs):
        raise _sdk_error(genai_errors.ServerError, 503)
    adapter.client.aio.files.delete = delete
    with pytest.raises(LLMUnavailableError):
        await adapter.delete_file(FileRef(name="files/abc", uri="u", expires_at=datetime.now(timezone.utc)))


@pytest.mark.asyncio
async def test_collect_without_keys_raises():
    with pytest.raises(ValueError):
        await _collect(_job([_inlined("w0")]), ())


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [types.JobState.JOB_STATE_RUNNING, types.JobState.JOB_STATE_FAILED,
                                   types.JobState.JOB_STATE_EXPIRED])
async def test_collect_on_unfinished_job_raises(state):
    with pytest.raises(ValueError):
        await _collect(_job([_inlined("w0")], state=state))


@pytest.mark.asyncio
async def test_collect_partially_succeeded_is_collected():
    out = await _collect(_job([_inlined("w0", text="A")], state=types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED))
    assert out["w0"].text == "A"


@pytest.mark.asyncio
async def test_submit_with_empty_job_name_raises():
    adapter = _adapter()

    async def create(**kwargs):
        return types.BatchJob(name=None)
    adapter.client.aio.batches.create = create
    with pytest.raises(LLMUnavailableError):
        await adapter.submit([_req()], display_name="x")


@pytest.mark.asyncio
async def test_collect_whole_call_errors_are_mapped():
    adapter = _adapter()

    async def boom(**kwargs):
        raise _sdk_error(genai_errors.ClientError, 429)
    adapter.client.aio.batches.get = boom
    with pytest.raises(LLMRateLimitError):
        await adapter.collect(BatchRef(name="b", keys=("w0",)))


@pytest.mark.asyncio
async def test_contract_rule_fails_for_json_schema_on_a_request():
    from tests.contracts.adapter_contracts import BATCH_SCHEMA_WITH_JSON_MIME as rule
    bad = types.InlinedRequest(model=MODEL, metadata={"key": "w0"}, config=types.GenerateContentConfig(
        response_json_schema={"type": "object"}, response_mime_type="application/json"))
    with pytest.raises(AssertionError):
        rule.validate("gemini_batch", {"src": [bad]})
    bad2 = types.InlinedRequest(model=MODEL, metadata={"key": "w0"}, config=types.GenerateContentConfig(
        response_schema=types.Schema(type="OBJECT")))
    with pytest.raises(AssertionError):  # schema without the JSON mime type
        rule.validate("gemini_batch", {"src": [bad2]})


def test_adapter_imports_no_other_adapter():
    import src.adapters.gemini_batch_adapter as mod
    text = open(mod.__file__).read()
    assert "gemini_adapter" not in text.replace("gemini_batch_adapter", "")
