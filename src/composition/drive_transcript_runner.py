"""
DriveTranscriptRunner — the transcriber agent over the batch port, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5, Δ102, Δ105, G28, G36).

Public interface:
    DriveTranscriptRunner(batch_port, prompt_builder, quota_service=None)
        async submit(inputs, *, display_name) -> BatchRef
            raises DriveModelUnavailable (a missing or incomplete prompt: no fallback prompt) and the provider
            outages `LLMUnavailableError` / `LLMRateLimitError` / `LLMAccountError` of the port, all uncharged
        async collect(batch, *, account_id) -> {request key: WindowResult}
            call it only after `poll` returned SUCCEEDED (the port raises ValueError otherwise)

`submit` builds the prompt once for the whole submission and turns every window into one request that carries the
uploaded file's NAME (never a URL). `collect` parses each result with the agent and bills the user's account at once,
priced by `BatchUsage.cost()` through `QuotaService.record_usage` (no execution billing scope spans hours, Δ105; no
(model, batch) `ModelUsage` leg, G36). A result without usage bills nothing and logs a WARNING (count only). Nothing
from a transcript, a file name or a URL is logged (§27.12). Which outcome costs the file an attempt is the stage's
decision (T-R18): `invalid` and a non-refused `error` are failed attempts, `refused` is a gap marker.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence

from ..agents.drive_transcriber_agent import AGENT_TYPE, DriveTranscriberAgent
from ..domain.agent import AgentConfig
from ..domain.batch_transcription import BatchRef, BatchResult
from ..domain.drive_transcript import WindowInput, WindowOutcome, WindowResult
from ..infrastructure.drive_model_call import DriveOutputInvalid, build_drive_prompt
from ..ports.batch_transcription_port import BatchTranscriptionPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..utils.logger import logger


class DriveTranscriptRunner:

    def __init__(self, batch_port: BatchTranscriptionPort, prompt_builder: PromptBuilderPort,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._port = batch_port
        self._prompt_builder = prompt_builder
        self._quota_service = quota_service
        self._agent = DriveTranscriberAgent(AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE,
                                                        capabilities=[AGENT_TYPE]))

    async def submit(self, inputs: Sequence[WindowInput], *, display_name: str) -> BatchRef:
        prompt = await build_drive_prompt(self._prompt_builder, AGENT_TYPE)   # built once per submission
        requests = [self._agent.build_request(i.window, prompt=prompt, key=i.key, file_name=i.file_name,
                                              file_mime=i.file_mime) for i in inputs]
        ref = await self._port.submit(requests, display_name=display_name)
        logger.info(f"[DriveTranscript] submitted {len(requests)} windows")
        return ref

    async def collect(self, batch: BatchRef, *, account_id: str) -> Mapping[str, WindowResult]:
        results = await self._port.collect(batch)
        out: Dict[str, WindowResult] = {}
        unbilled = 0
        for key, result in results.items():
            usd = await self._bill(account_id, result)
            if result.usage is None:
                unbilled += 1
            out[key] = self._classify(key, result, usd)
        if unbilled:
            logger.warning(f"[DriveTranscript] {unbilled} of {len(results)} results came without usage: nothing billed")
        counts: Dict[str, int] = {}
        for r in out.values():
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        logger.info(f"[DriveTranscript] collected {len(out)} windows {counts} cost=${sum(r.usd for r in out.values()):.5f}")
        return out

    def _classify(self, key: str, result: BatchResult, usd: float) -> WindowResult:
        if result.refused:
            return WindowResult(key=key, outcome=WindowOutcome.REFUSED, usd=usd)
        if result.text is None:
            return WindowResult(key=key, outcome=WindowOutcome.ERROR, transient=result.transient, usd=usd)
        try:
            transcript = self._agent.parse(result.text)
        except DriveOutputInvalid:
            return WindowResult(key=key, outcome=WindowOutcome.INVALID, usd=usd)
        return WindowResult(key=key, outcome=WindowOutcome.OK, transcript=transcript, usd=usd)

    async def _bill(self, account_id: str, result: BatchResult) -> float:
        """Books one result's batch cost to the account; never raises (a failed write must not lose the answer)."""
        usage = result.usage
        if usage is None or self._quota_service is None:
            return 0.0
        if not usage.is_priced():
            logger.warning(f"[DriveTranscript] model {usage.model} has no price: nothing billed")
            return 0.0
        cost = usage.cost()
        tokens = usage.audio_input_tokens + usage.text_input_tokens + usage.output_tokens + usage.thought_tokens
        try:
            await self._quota_service.record_usage(account_id, usage.model, tokens, cost)
        except Exception as exc:
            logger.error(f"[DriveTranscript] billing write failed: {type(exc).__name__}: {exc}")
            return 0.0
        return cost
