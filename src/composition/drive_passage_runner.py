"""
DrivePassageRunner — the passages agent as a PassageHintPort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.2, §25.4.4, §25.4.6).

Public interface:
    DrivePassageRunner(context_builder, user_repo, prompt_builder, quota_service=None)
        async hints(request) -> HintReply                    never raises

Per call it builds the agent from the user's configuration and bills to `request.account_id` (the DriveReaderRunner
pattern). The request's chunks go in one call; then, per chunk, an outcome (`HintReply.outcome_by_index`):
- a batch stopped at the output cap is split in halves and asked again; a single chunk still stopped gets no hints
  (`stopped_at_cap`, counted);
- a batch the primary refuses (400/413/422, a safety stop) is halved down to the chunk; a chunk still refused gets
  no hints (`refused`) and never holds the file;
- output still invalid after the parse retry, or a chunk left out after the correction turn: `invalid` (charged);
- a 5xx, a timeout, an unexpected error or the fallback's refusal: the chunks of that call `failed`, and no further
  call is made (the agent type backs off);
- an outage for everyone (429, network, breaker, account 4xx, missing key or prompt): no further call, and the chunks
  not settled yet have no outcome (retried).
`status` is the batch's deciding effect: `unavailable` before `failed` before `invalid` / `stopped_at_cap`, else `ok`.
Items of `ok` chunks are kept whatever `status` says, ungrounded (the caller runs `drive_hints.ground`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..agents.drive_passage_agent import AGENT_TYPE, DrivePassageAgent
from ..domain.drive_enrichment import HintItem
from ..domain.drive_enrichment_calls import HintReply, HintRequest, ModelReplyStatus
from ..domain.drive_extraction import DriveModelUnavailable
from ..infrastructure.drive_model_call import DriveModelCallFailed, reply_status
from ..ports.passage_hint_port import PassageHintPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger
from .drive_agent_build import build_drive_agent

S = ModelReplyStatus
_CHARGED_ORDER = (S.UNAVAILABLE, S.FAILED, S.INVALID, S.STOPPED_AT_CAP)


@dataclass
class _Run:
    items: Dict[int, Tuple[HintItem, ...]] = field(default_factory=dict)
    outcomes: Dict[int, ModelReplyStatus] = field(default_factory=dict)
    halted: Optional[ModelReplyStatus] = None     # unavailable / failed: no further call
    model: str = ""
    usd: float = 0.0
    fallback_usd: float = 0.0
    calls: int = 0

    def spent(self, model: str, usd: float, fallback_usd: float, calls: int) -> None:
        self.model = model or self.model
        self.usd += usd
        self.fallback_usd += fallback_usd
        self.calls += calls


class DrivePassageRunner(PassageHintPort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._quota_service = quota_service

    async def hints(self, request: HintRequest) -> HintReply:
        if not request.chunks:
            return HintReply(status=S.OK, items_by_index={}, model="", usd=0.0)
        try:
            agent = await build_drive_agent(DrivePassageAgent, AGENT_TYPE, request.user_id,
                                            context_builder=self._context_builder, user_repo=self._user_repo,
                                            prompt_builder=self._prompt_builder, quota_service=self._quota_service)
        except DriveModelUnavailable:
            return HintReply(status=S.UNAVAILABLE, items_by_index={}, model="", usd=0.0)
        run = _Run()
        await self._batch(agent, request, list(request.chunks), run)
        status = _status(run)
        logger.info(f"[DrivePassages] user={request.user_id[:8]} item={request.item_id} chunks={len(request.chunks)} "
                    f"status={status.value} outcomes={sorted((i, o.value) for i, o in run.outcomes.items())} "
                    f"calls={run.calls} model={run.model or '-'} cost=${run.usd:.5f} "
                    f"fallback=${run.fallback_usd:.5f}")
        return HintReply(status=status, items_by_index=dict(run.items), model=run.model, usd=round(run.usd, 6),
                         fallback_usd=round(run.fallback_usd, 6), outcome_by_index=dict(run.outcomes))

    async def _batch(self, agent: DrivePassageAgent, request: HintRequest, chunks: List[Tuple[int, str]],
                     run: _Run) -> None:
        if run.halted is not None or not chunks:
            return
        indexes = [index for index, _ in chunks]
        try:
            answer = await agent.hints(chunks, title=request.title, what_it_is=request.what_it_is,
                                       account_id=request.account_id)
        except (DriveModelCallFailed, DriveModelUnavailable) as exc:
            run.spent(getattr(exc, "model_name", ""), getattr(exc, "model_cost", 0.0),
                      getattr(exc, "model_fallback_cost", 0.0), getattr(exc, "model_calls", 0))
            status = reply_status(exc)
            if status is S.REFUSED:
                if len(chunks) == 1:
                    logger.warning(f"[DrivePassages] chunk {indexes[0]} of {request.item_id} refused; no hints")
                    run.outcomes[indexes[0]] = S.REFUSED
                    return
                await self._halves(agent, request, chunks, run)
                return
            if status is S.UNAVAILABLE:
                run.halted = S.UNAVAILABLE
                return
            for index in indexes:
                run.outcomes[index] = status
            if status is S.FAILED:
                run.halted = S.FAILED
            return
        except Exception as exc:   # never raise across the port
            logger.error(f"[DrivePassages] unexpected failure: {type(exc).__name__}: {exc}", exc_info=exc)
            for index in indexes:
                run.outcomes[index] = S.FAILED
            run.halted = S.FAILED
            return
        run.spent(answer.model, answer.usd, answer.fallback_usd, answer.calls)
        for index, items in answer.items_by_index.items():
            run.items[index] = tuple(items)
            run.outcomes[index] = S.OK
        if answer.output_limited:
            left = [c for c in chunks if c[0] not in answer.items_by_index]
            if len(left) == 1 and len(chunks) == 1:
                logger.warning(f"[DrivePassages] chunk {indexes[0]} of {request.item_id} still at the output cap; "
                               f"no hints")
                run.outcomes[indexes[0]] = S.STOPPED_AT_CAP
                return
            await self._halves(agent, request, left, run)
            return
        for index in answer.missing:
            run.outcomes[index] = S.INVALID

    async def _halves(self, agent: DrivePassageAgent, request: HintRequest, chunks: Sequence[Tuple[int, str]],
                      run: _Run) -> None:
        if len(chunks) == 1:
            await self._batch(agent, request, list(chunks), run)
            return
        mid = len(chunks) // 2
        await self._batch(agent, request, list(chunks[:mid]), run)
        await self._batch(agent, request, list(chunks[mid:]), run)


def _status(run: _Run) -> ModelReplyStatus:
    if run.halted is S.UNAVAILABLE:
        return S.UNAVAILABLE
    for status in _CHARGED_ORDER[1:]:
        if status in run.outcomes.values():
            return status
    return S.OK
