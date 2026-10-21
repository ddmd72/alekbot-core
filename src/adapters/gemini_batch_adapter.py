"""
GeminiBatchAdapter — BatchTranscriptionPort over the Gemini Batch API (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5).

Public interface:
    GeminiBatchAdapter(api_key, model, *, file_base_url=..., call_timeout_s=..., upload_timeout_s=...)
    upload_audio / submit / find_batch / poll / collect / delete_file  (see BatchTranscriptionPort)

Batch rules (Δ131, measured on google-genai 2.17.0):
  * the schema goes as a typed `response_schema` with `response_mime_type="application/json"`; an inline request's
    `response_json_schema` is ignored silently;
  * `thinking_level=LOW` on every request;
  * the request key rides in `InlinedRequest.metadata` and is read back from `InlinedResponse.metadata`; order is
    never trusted;
  * a SUCCEEDED job can hold an error inside one response: that request is an error result;
  * usage is read per modality; thought tokens are billed into the output leg.

Errors (G25): 429, 5xx, timeouts, network and account 4xx (auth, permission, billing) raise
LLMRateLimitError / LLMUnavailableError / LLMAccountError (an uncharged outage for the caller); any other 4xx is an
LLMClientError. A request the provider refuses for its content is that request's result with `refused=True`.
Nothing here logs text, a file URI or an id the caller passed: display names are hashed, logs carry counts and states.
The model id comes from the constructor (G26); this module imports no other adapter (REQ-ARCH-23).
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, TypeVar

import aiohttp
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..domain.batch_transcription import BatchRef, BatchRequest, BatchResult, BatchStatus, FileRef
from ..domain.billing import BatchUsage
from ..domain.exceptions import (
    BatchLookupInconclusive,
    LLMAccountError,
    LLMClientError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from ..ports.batch_transcription_port import BatchTranscriptionPort
from ..utils.logger import logger

T = TypeVar("T")

DEFAULT_FILE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_FILE_TTL = timedelta(hours=48)  # the Files API keeps an input for 48 h; used when the SDK returns no expiry
FIND_PAGE_SIZE = 100            # find_batch: jobs per listed page
FIND_MAX_JOBS = 2_000           # find_batch: jobs scanned at most (weeks of this project's batches); past it: inconclusive (LLMUnavailableError)

_STATES: Dict[Any, BatchStatus] = {
    types.JobState.JOB_STATE_UNSPECIFIED: BatchStatus.PENDING,
    types.JobState.JOB_STATE_QUEUED: BatchStatus.PENDING,
    types.JobState.JOB_STATE_PENDING: BatchStatus.PENDING,
    types.JobState.JOB_STATE_RUNNING: BatchStatus.RUNNING,
    types.JobState.JOB_STATE_UPDATING: BatchStatus.RUNNING,
    types.JobState.JOB_STATE_PAUSED: BatchStatus.RUNNING,
    types.JobState.JOB_STATE_CANCELLING: BatchStatus.RUNNING,
    types.JobState.JOB_STATE_SUCCEEDED: BatchStatus.SUCCEEDED,
    types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED: BatchStatus.SUCCEEDED,
    types.JobState.JOB_STATE_FAILED: BatchStatus.FAILED,
    types.JobState.JOB_STATE_CANCELLED: BatchStatus.CANCELLED,
    types.JobState.JOB_STATE_EXPIRED: BatchStatus.EXPIRED,
}

_THINKING = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}

# Finish reasons that are the provider declining the content, not a stall or a cut.
_REFUSAL_FINISH = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
                             "IMAGE_PROHIBITED_CONTENT", "RECITATION"})

# A 4xx whose body is about the account, not the request. Status alone cannot tell it from a refused request.
_ACCOUNT_MARKERS = ("billing", "quota", "credit", "api key", "permission", "unauthenticated", "spending cap")

# google.rpc.Code values a later attempt can clear: DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, INTERNAL, UNAVAILABLE.
_TRANSIENT_RPC_CODES = frozenset({4, 8, 13, 14})

# ErrorInfo.reason values and statuses that mean the account or key, not the request.
_ACCOUNT_REASONS = frozenset({"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED", "BILLING_DISABLED",
                              "CONSUMER_SUSPENDED", "SERVICE_DISABLED", "ACCESS_TOKEN_EXPIRED",
                              "ACCOUNT_STATE_INVALID", "CREDENTIALS_MISSING", "RATE_LIMIT_EXCEEDED_BILLING"})
_ACCOUNT_STATUSES = frozenset({"UNAUTHENTICATED", "PERMISSION_DENIED"})


def _hash(text: str) -> str:
    return "tr-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


class GeminiBatchAdapter(BatchTranscriptionPort):
    def __init__(self, api_key: str, model: str, *, file_base_url: str = DEFAULT_FILE_BASE_URL,
                 call_timeout_s: float = 120.0, upload_timeout_s: float = 600.0) -> None:
        self.client = genai.Client(api_key=api_key)
        self._model = model
        self._file_base_url = file_base_url.rstrip("/")
        self._call_timeout_s = call_timeout_s
        self._upload_timeout_s = upload_timeout_s

    # ---------------------------------------------------------------- port

    async def upload_audio(self, path: str, mime: str) -> FileRef:
        config = types.UploadFileConfig(mime_type=mime, display_name=_hash(path))
        uploaded = await self._call("upload", self._upload_timeout_s,
                                    lambda: self.client.aio.files.upload(file=path, config=config))
        expires = uploaded.expiration_time or (datetime.now(timezone.utc) + _FILE_TTL)
        logger.info("[GeminiBatch] uploaded audio file (mime=%s)", mime)
        return FileRef(name=uploaded.name or "", uri=uploaded.uri or "", expires_at=expires)

    async def submit(self, requests: Sequence[BatchRequest], *, display_name: str) -> BatchRef:
        inlined = [self._to_inlined(r) for r in requests]
        config = types.CreateBatchJobConfig(display_name=_hash(display_name))
        job = await self._call("submit", self._call_timeout_s,
                               lambda: self.client.aio.batches.create(model=self._model, src=inlined, config=config))
        if not job.name:
            raise LLMUnavailableError("submit: the provider returned a job without a name")
        logger.info("[GeminiBatch] submitted batch (requests=%d)", len(inlined))
        return BatchRef(name=job.name or "", keys=tuple(r.key for r in requests))

    async def find_batch(self, display_name: str) -> Optional[BatchRef]:
        wanted = _hash(display_name)   # the name submit() sent; the raw one never reaches the provider

        async def scan() -> Optional[types.BatchJob]:
            pager = await self.client.aio.batches.list(config=types.ListBatchJobsConfig(page_size=FIND_PAGE_SIZE))
            scanned = 0
            async for job in pager:
                if job.display_name == wanted:
                    return job
                scanned += 1
                if scanned >= FIND_MAX_JOBS:
                    # Inconclusive, not a miss: the job may lie past the bound (jobs are never deleted, the list only
                    # grows). The stage treats it as an outage and keeps the money.
                    logger.warning("[GeminiBatch] find: scan bound reached (jobs=%d), inconclusive", scanned)
                    raise BatchLookupInconclusive("find: scan bound")
            return None

        job = await self._call("find", self._call_timeout_s, scan)
        found = job is not None and bool(job.name)
        logger.info("[GeminiBatch] find by display name: %s", "found" if found else "not found")
        return BatchRef(name=job.name or "") if job is not None and job.name else None

    async def poll(self, batch: BatchRef) -> BatchStatus:
        job = await self._call("poll", self._call_timeout_s, lambda: self.client.aio.batches.get(name=batch.name))
        status = _STATES.get(job.state, BatchStatus.RUNNING)
        logger.info("[GeminiBatch] poll state=%s", status.value)
        return status

    async def collect(self, batch: BatchRef) -> Mapping[str, BatchResult]:
        if not batch.keys:
            raise ValueError("collect needs the request keys; a BatchRef rebuilt without them would drop paid results")
        job = await self._call("collect", self._call_timeout_s, lambda: self.client.aio.batches.get(name=batch.name))
        state = _STATES.get(job.state, BatchStatus.RUNNING)
        if state is not BatchStatus.SUCCEEDED:
            raise ValueError(f"collect on a job that is {state.value}, not succeeded")
        answers = list(getattr(job.dest, "inlined_responses", None) or [])
        expected = set(batch.keys)
        results: Dict[str, BatchResult] = {}
        for answer in answers:
            key = (answer.metadata or {}).get("key", "")
            if key not in expected or key in results:
                # Never fall back on position: an answer we cannot attribute is dropped, its slot fails below.
                logger.warning("[GeminiBatch] answer with unknown or repeated key dropped")
                continue
            results[key] = self._to_result(key, answer)
        for key in batch.keys:
            if key not in results:
                results[key] = BatchResult(key=key, error="key mismatch: no answer carried this request key")
        logger.info("[GeminiBatch] collected answers=%d expected=%d failed=%d", len(answers), len(batch.keys),
                    sum(1 for r in results.values() if r.error))
        return results

    async def delete_file(self, ref: FileRef) -> None:
        try:
            await self._call("delete", self._call_timeout_s, lambda: self.client.aio.files.delete(name=ref.name))
        except LLMAccountError as e:
            if e.http_status == 403:
                logger.info("[GeminiBatch] file already gone")
                return
            raise
        except LLMClientError as e:
            if e.http_status in (403, 404):  # the provider answers 403 for a file that no longer exists
                logger.info("[GeminiBatch] file already gone")
                return
            raise

    # ------------------------------------------------------------ requests

    def _to_inlined(self, req: BatchRequest) -> types.InlinedRequest:
        parts: List[types.Part] = []
        if req.file_name:
            parts.append(types.Part(file_data=types.FileData(
                file_uri=f"{self._file_base_url}/{req.file_name}", mime_type=req.file_mime)))
        parts.append(types.Part(text=req.user_text))
        schema = types.Schema.model_validate(dict(req.response_schema)) if req.response_schema else None
        config = types.GenerateContentConfig(
            system_instruction=req.system_instruction or None,
            max_output_tokens=req.max_output_tokens,
            response_schema=schema,
            response_mime_type="application/json" if schema is not None else None,
            thinking_config=types.ThinkingConfig(thinking_level=_THINKING.get(req.thinking_level, types.ThinkingLevel.LOW)),
        )
        return types.InlinedRequest(model=self._model, contents=[types.Content(role="user", parts=parts)],
                                    metadata={"key": req.key}, config=config)

    # ------------------------------------------------------------- results

    def _to_result(self, key: str, answer: types.InlinedResponse) -> BatchResult:
        if answer.error is not None:
            # A finished batch's results are immutable: a response error never raises (one bad response must not make
            # every later collect fail). The code is surfaced; the caller charges the attempt. The message may quote
            # input, so only the code is kept.
            code = int(answer.error.code or 0)
            return BatchResult(key=key, error=f"response error code {code}", code=code,
                               transient=code in _TRANSIENT_RPC_CODES)
        response = answer.response
        if response is None:
            return BatchResult(key=key, error="answer without a response")
        usage = self._usage(response)
        block = getattr(response.prompt_feedback, "block_reason", None)
        if block is not None:
            return BatchResult(key=key, error=f"prompt blocked: {block}", refused=True, usage=usage,
                               finish_reason="BLOCKED")
        candidate = (response.candidates or [None])[0]
        finish = self._finish(candidate)
        text = self._text(candidate)
        if finish in _REFUSAL_FINISH:
            return BatchResult(key=key, error=f"refused: {finish}", refused=True, usage=usage, finish_reason=finish)
        if not text:
            return BatchResult(key=key, error=f"empty answer (finish={finish or 'none'})", usage=usage,
                               finish_reason=finish)
        return BatchResult(key=key, text=text, usage=usage, finish_reason=finish)

    @staticmethod
    def _finish(candidate: Optional[types.Candidate]) -> str:
        reason = getattr(candidate, "finish_reason", None)
        return getattr(reason, "value", None) or (str(reason) if reason else "")

    @staticmethod
    def _text(candidate: Optional[types.Candidate]) -> str:
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []
        return "".join(p.text for p in parts if p.text and not p.thought)

    def _usage(self, response: types.GenerateContentResponse) -> Optional[BatchUsage]:
        meta = response.usage_metadata
        if meta is None:
            return None
        audio = text = 0
        for detail in meta.prompt_tokens_details or []:
            if detail.modality == types.MediaModality.AUDIO:
                audio += detail.token_count or 0
            else:
                text += detail.token_count or 0  # TEXT, and any modality we do not price apart
        if audio + text == 0:
            text = meta.prompt_token_count or 0
        return BatchUsage(model=self._model, audio_input_tokens=audio, text_input_tokens=text,
                          output_tokens=meta.candidates_token_count or 0, thought_tokens=meta.thoughts_token_count or 0)

    # -------------------------------------------------------------- errors

    async def _call(self, what: str, timeout_s: float, make: Callable[[], Awaitable[T]]) -> T:
        try:
            return await asyncio.wait_for(make(), timeout=timeout_s)
        except asyncio.TimeoutError as e:
            logger.warning("[GeminiBatch] %s timed out", what)
            raise LLMUnavailableError(f"{what}: timed out after {timeout_s:.0f}s") from e
        except genai_errors.ClientError as e:
            code = getattr(e, "code", None)
            logger.warning("[GeminiBatch] %s client error status=%s", what, code)
            if code == 429:
                raise LLMRateLimitError(f"{what}: rate limited", http_status=429) from e
            if code in (401, 403) or self._is_account_error(e):
                raise LLMAccountError(f"{what}: account rejected the call", http_status=code) from e
            raise LLMClientError(f"{what}: client error", http_status=code if isinstance(code, int) else None) from e
        except genai_errors.ServerError as e:
            code = getattr(e, "code", None)
            logger.warning("[GeminiBatch] %s server error status=%s", what, code)
            raise LLMUnavailableError(f"{what}: provider error", http_status=code if isinstance(code, int) else None) from e
        except (httpx.TimeoutException, httpx.NetworkError, aiohttp.ClientError) as e:
            logger.warning("[GeminiBatch] %s transport failure (%s)", what, type(e).__name__)
            raise LLMUnavailableError(f"{what}: transport failure") from e

    @staticmethod
    def _is_account_error(e: genai_errors.APIError) -> bool:
        """Structured fields first (ErrorInfo.reason, then the status); keywords only when neither says."""
        error = e.details.get("error", e.details) if isinstance(e.details, dict) else {}
        infos = error.get("details", []) if isinstance(error, dict) else []
        reasons = {i.get("reason") for i in infos if isinstance(i, dict) and i.get("reason")}
        if reasons & _ACCOUNT_REASONS:
            return True
        status = getattr(e, "status", None)
        if status in _ACCOUNT_STATUSES:
            return True
        text = (e.message or str(e)).lower()
        return status == "FAILED_PRECONDITION" or any(m in text for m in _ACCOUNT_MARKERS)
