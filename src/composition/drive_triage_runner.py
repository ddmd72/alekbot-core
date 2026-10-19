"""
DriveTriageRunner — the image triage agent as an ImageTriagePort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.2, §24.5; triage v2: §25.4.3, §25.4.4, §25.4.6, §25.13.1).

Public interface:
    DriveTriageRunner(context_builder, user_repo, prompt_builder, quota_service=None)
        async triage(user_id, account_id, images) -> TriageReply          never raises

Per call it builds the agent from the user's UserBotConfig through AgentContextBuilder and bills to `account_id`
(no prompt-content store, §24.2). Images go in batches of TRIAGE_BATCH, each thumbnail under an opaque index 1..n.
One `TriageResultV2` per image answered (a text image carries triage's capture, §26.3):
- a thumbnail the providers do not take (type, or over MAX_VISION_IMAGE_BYTES) is never sent: `refused_item_ids`;
- a batch stopped at the output cap keeps the answers received and asks again, halved, for the rest; a batch refused
  by the primary (400/413/422, a safety stop) is halved down to the image; an image still stopped / refused alone goes to `capped_item_ids` / `refused_item_ids` (no description);
- output still invalid after the correction turn, an image left out or still disagreeing with its identical twin:
  unsettled (no result, retried), status `invalid`;
- a 5xx, a timeout, an unexpected error or the fallback's refusal: `failed`, and no further call; an outage for
  everyone (429, network, breaker, account 4xx, missing key or prompt, no user): `unavailable`, no further call.
`status` is the deciding effect (`unavailable` before `failed` before `invalid` / `stopped_at_cap`, else `ok`;
refusals alone leave it `ok`), with the calls made, the model that served, the cost and the fallback's share.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..agents.drive_image_triage_agent import AGENT_TYPE, DriveImageTriageAgent, TriageInput
from ..domain.drive_enrichment import PhotoDescription
from ..domain.drive_enrichment_calls import ModelReplyStatus, TriageReply, TriageResultV2
from ..domain.drive_extraction import DriveModelUnavailable, TriageImage
from ..domain.drive_image_mime import MAX_VISION_IMAGE_BYTES, VISION_MIME_TYPES, image_mime
from ..domain.drive_index import TriageVerdict
from ..domain.drive_index_params import TRIAGE_BATCH
from ..infrastructure.drive_model_call import DriveModelCallFailed, reply_status
from ..ports.image_triage_port import ImageTriagePort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger
from .drive_agent_build import build_drive_agent

S = ModelReplyStatus


@dataclass
class _Run:
    results: Dict[int, Tuple[TriageVerdict, Optional[PhotoDescription]]] = field(default_factory=dict)
    captures: Dict[int, str] = field(default_factory=dict)         # text images: what triage said of the capture
    capped: Set[int] = field(default_factory=set)
    refused: Set[int] = field(default_factory=set)
    charged: Set[ModelReplyStatus] = field(default_factory=set)   # invalid / stopped_at_cap seen
    halted: Optional[ModelReplyStatus] = None                      # unavailable / failed: no further call
    model: str = ""
    usd: float = 0.0
    fallback_usd: float = 0.0
    calls: int = 0

    def spent(self, model: str, usd: float, fallback_usd: float, calls: int) -> None:
        self.model = model or self.model
        self.usd += usd
        self.fallback_usd += fallback_usd
        self.calls += calls


class DriveTriageRunner(ImageTriagePort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._quota_service = quota_service

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        run = _Run()
        inputs: List[TriageInput] = []
        for index, image in enumerate(images, start=1):
            # Magic bytes first: a rendition is JPEG or PNG whatever the declared type (S0f).
            mime = image_mime(image.thumbnail[:16]) or image.mime_type
            if mime in VISION_MIME_TYPES and len(image.thumbnail) <= MAX_VISION_IMAGE_BYTES:
                inputs.append(TriageInput(index=index, data=image.thumbnail, mime_type=mime,
                                          camera_prior=image.camera_prior))
            else:
                run.refused.add(index)   # no provider takes it: the same bytes always get the same answer
        if inputs:
            try:
                agent = await build_drive_agent(DriveImageTriageAgent, AGENT_TYPE, user_id,
                                                context_builder=self._context_builder, user_repo=self._user_repo,
                                                prompt_builder=self._prompt_builder,
                                                quota_service=self._quota_service)
            except DriveModelUnavailable:
                run.halted = S.UNAVAILABLE
            else:
                for start in range(0, len(inputs), TRIAGE_BATCH):
                    await self._batch(agent, inputs[start:start + TRIAGE_BATCH], account_id, run)
        by_index = {index: image.item_id for index, image in enumerate(images, start=1)}
        results = tuple(TriageResultV2(item_id=by_index[i], verdict=verdict, photo=photo,
                                       capture=run.captures.get(i, ""))
                        for i, (verdict, photo) in sorted(run.results.items()))
        status = _status(run)
        logger.info(f"[DriveTriage] user={user_id[:8]} images={len(images)} sent={len(inputs)} "
                    f"text={sum(r.verdict is TriageVerdict.TEXT for r in results)} results={len(results)} "
                    f"capped={len(run.capped)} refused={len(run.refused)} status={status.value} calls={run.calls} "
                    f"model={run.model or '-'} cost=${run.usd:.4f} fallback=${run.fallback_usd:.4f}")
        return TriageReply(status=status, results=results, model=run.model, usd=round(run.usd, 6),
                           fallback_usd=round(run.fallback_usd, 6), model_calls=run.calls,
                           capped_item_ids=tuple(by_index[i] for i in sorted(run.capped)),
                           refused_item_ids=tuple(by_index[i] for i in sorted(run.refused)))

    async def _batch(self, agent: DriveImageTriageAgent, inputs: List[TriageInput], account_id: str,
                     run: _Run) -> None:
        if run.halted is not None or not inputs:
            return
        try:
            answer = await agent.classify(inputs, account_id=account_id)
        except (DriveModelCallFailed, DriveModelUnavailable) as exc:
            run.spent(getattr(exc, "model_name", ""), getattr(exc, "model_cost", 0.0),
                      getattr(exc, "model_fallback_cost", 0.0), getattr(exc, "model_calls", 0))
            status = reply_status(exc)
            if status is S.REFUSED:
                if len(inputs) == 1:
                    logger.warning(f"[DriveTriage] image refused ({exc}); no description")
                    run.refused.add(inputs[0].index)
                    return
                await self._halves(agent, inputs, account_id, run)
            elif status in (S.UNAVAILABLE, S.FAILED):
                run.halted = status
            else:
                run.charged.add(status)   # invalid: the batch's images stay unsettled
            return
        except Exception as exc:   # never raise across the port
            logger.error(f"[DriveTriage] unexpected failure: {type(exc).__name__}: {exc}", exc_info=exc)
            run.halted = S.FAILED
            return
        run.spent(answer.model, answer.usd, answer.fallback_usd, answer.calls)
        run.results.update(answer.results)
        run.captures.update(answer.captures)
        if answer.output_limited:
            left = [i for i in inputs if i.index not in answer.results]
            if len(inputs) == 1 and left:
                logger.warning("[DriveTriage] one image still at the output cap; no description")
                run.capped.add(inputs[0].index)
                run.charged.add(S.STOPPED_AT_CAP)
                return
            if len(left) == 1:
                await self._batch(agent, left, account_id, run)
            elif left:
                await self._halves(agent, left, account_id, run)
            return
        if answer.missing:
            run.charged.add(S.INVALID)

    async def _halves(self, agent: DriveImageTriageAgent, inputs: List[TriageInput], account_id: str,
                      run: _Run) -> None:
        mid = len(inputs) // 2
        await self._batch(agent, inputs[:mid], account_id, run)
        await self._batch(agent, inputs[mid:], account_id, run)


def _status(run: _Run) -> ModelReplyStatus:
    if run.halted is not None:
        return run.halted
    for status in (S.INVALID, S.STOPPED_AT_CAP):
        if status in run.charged:
            return status
    return S.OK
