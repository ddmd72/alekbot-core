"""
DriveTranscriptRunner — the transcriber agent over the batch port, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.5, Δ102, Δ105, G28, G36).

Public interface:
    DriveTranscriptRunner(batch_port, prompt_builder, quota_service=None)
        async submit(inputs, *, display_name) -> BatchRef
            raises DriveModelUnavailable (a missing or incomplete prompt: no fallback prompt) and the provider
            outages `LLMUnavailableError` / `LLMRateLimitError` / `LLMAccountError` of the port, all uncharged
        async collect(batch, *, account_id, bill=True) -> {request key: WindowResult}
            call it only after `poll` returned SUCCEEDED (the port raises ValueError otherwise); `bill=False` (a
            re-collection of a batch whose cost was booked already, T-R22) writes nothing to the account, `usd` is
            still reported

`submit` builds the prompt once for the whole submission and turns every window into one request that carries the
uploaded file's NAME (never a URL). `collect` parses each result with the agent and bills the user's account at once,
priced by `BatchUsage.cost()` (reported as `WindowResult.usd` whatever the account write does) through `QuotaService.record_usage` (no execution billing scope spans hours, Δ105; no
(model, batch) `ModelUsage` leg, G36). A result without usage bills nothing and logs a WARNING (count only); results
whose text input passed the reservation's per-window allowance (TRANSCRIBE_WINDOW_PROMPT_TOKENS) log one ERROR per
collect (counts only). Nothing
from a transcript, a file name or a URL is logged (§27.12). Which outcome costs the file an attempt is the stage's
decision (T-R18): `invalid` and a non-refused `error` are failed attempts, `refused` is a gap marker.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional, Sequence, Tuple

from ..agents.drive_transcriber_agent import AGENT_TYPE, DriveTranscriberAgent
from ..domain.agent import AgentConfig
from ..domain.batch_transcription import BatchRef, BatchResult
from ..domain.drive_index_params import TRANSCRIBE_WINDOW_PROMPT_TOKENS
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

    async def collect(self, batch: BatchRef, *, account_id: str, bill: bool = True) -> Mapping[str, WindowResult]:
        results = await self._port.collect(batch)
        out: Dict[str, WindowResult] = {}
        unbilled = 0
        unpriced = 0
        overrun = 0
        widest = 0
        finishes: Dict[str, int] = {}
        for key, result in results.items():
            cost, billed = await self._bill(account_id, result, write=bill)
            if result.usage is None:
                unbilled += 1
            elif not result.usage.is_priced():
                unpriced += 1
            if result.usage is not None and result.usage.text_input_tokens > TRANSCRIBE_WINDOW_PROMPT_TOKENS:
                overrun += 1
                widest = max(widest, result.usage.text_input_tokens)
            finishes[result.finish_reason or "none"] = finishes.get(result.finish_reason or "none", 0) + 1
            out[key] = self._classify(key, result, cost, billed)
        if unbilled:
            logger.warning(f"[DriveTranscript] {unbilled} of {len(results)} results came without usage: nothing billed")
        if unpriced:
            logger.error(f"[DriveTranscript] {unpriced} of {len(results)} results used a model with no price: cost 0")
        if overrun:   # the reservation allows TRANSCRIBE_WINDOW_PROMPT_TOKENS of text input per window (T-R19 N2)
            logger.error(f"[DriveTranscript] {overrun} of {len(results)} results used more text input than the "
                         f"reserved allowance ({widest} > {TRANSCRIBE_WINDOW_PROMPT_TOKENS} tokens): raise it")
        counts: Dict[str, int] = {}
        for r in out.values():
            counts[r.outcome.value] = counts.get(r.outcome.value, 0) + 1
        logger.info(f"[DriveTranscript] collected {len(out)} windows {counts} finish={finishes} "
                    f"cost=${sum(r.usd for r in out.values()):.5f}")
        return out

    def _classify(self, key: str, result: BatchResult, usd: float, billed: bool) -> WindowResult:
        if result.refused:
            return WindowResult(key=key, outcome=WindowOutcome.REFUSED, usd=usd, billed=billed)
        if result.text is None:
            return WindowResult(key=key, outcome=WindowOutcome.ERROR, transient=result.transient, usd=usd, billed=billed,
                                code=result.code)
        try:
            transcript = self._agent.parse(result.text)
        except DriveOutputInvalid:
            return WindowResult(key=key, outcome=WindowOutcome.INVALID, usd=usd, billed=billed,
                                finish_reason=result.finish_reason)
        return WindowResult(key=key, outcome=WindowOutcome.OK, transcript=transcript, usd=usd, billed=billed,
                            finish_reason=result.finish_reason)

    async def _bill(self, account_id: str, result: BatchResult, *, write: bool = True) -> Tuple[float, bool]:
        """(priced cost, whether the account write succeeded). The cost is independent of the write; the write is a
        side effect and never raises (a failed one must not lose the answer). `write=False`: priced, never written."""
        usage = result.usage
        if usage is None or not usage.is_priced():
            return 0.0, False
        cost = usage.cost()
        if self._quota_service is None or not write:
            return cost, False
        tokens = usage.audio_input_tokens + usage.text_input_tokens + usage.output_tokens + usage.thought_tokens
        try:
            await self._quota_service.record_usage(account_id, usage.model, tokens, cost)
        except Exception as exc:
            logger.error(f"[DriveTranscript] billing write failed: {type(exc).__name__}: {exc}")
            return cost, False
        return cost, True
