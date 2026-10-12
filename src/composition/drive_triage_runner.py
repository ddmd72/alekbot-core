"""
DriveTriageRunner — the image triage agent as an ImageTriagePort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.2, §24.5, §24.8, §24.9).

Public interface:
    DriveTriageRunner(context_builder, user_repo, prompt_builder, quota_service=None)
        async triage(user_id, account_id, images) -> TriageBatchOutcome     raises DriveModelUnavailable only

Per batch it builds the agent from the user's UserBotConfig through AgentContextBuilder, sets the quota service and
bills to `account_id` (the CompanionExtractorRunner pattern; no prompt-content store, §24.2). Each image gets an
opaque index 1..n. One result per image given, always, with the calls made and their cost (§24.9):
- a batch the provider refuses (HTTP 400) is halved until the refused image is alone; that image, an image the
  model left unanswered after its correction turn, and a rendition no provider takes are photographs without a
  caption — the same deterministic answer as an image without a rendition (§24.5);
- an outage (429, 5xx, timeout, a missing prompt) raises DriveModelUnavailable for the whole batch (§24.8),
  carrying the images already answered, the calls made and their cost, for the tick to count (§24.7, §24.9).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..agents.drive_image_triage_agent import AGENT_TYPE, DriveImageTriageAgent, TriageInput
from ..infrastructure.drive_model_call import DriveModelCallFailed
from ..domain.agent import AgentConfig
from ..domain.drive_extraction import DriveModelUnavailable, TriageBatchOutcome, TriageImage, TriageResult
from ..domain.drive_image_mime import VISION_MIME_TYPES, image_mime
from ..domain.drive_index import TriageVerdict
from ..ports.image_triage_port import ImageTriagePort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

_Verdicts = Dict[int, Tuple[TriageVerdict, str]]


class DriveTriageRunner(ImageTriagePort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._quota_service = quota_service

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageBatchOutcome:
        if not images:
            return TriageBatchOutcome(results=())
        inputs: List[TriageInput] = []
        for index, image in enumerate(images, start=1):
            # Magic bytes first: a rendition is JPEG or PNG whatever the declared type (S0f).
            mime = image_mime(image.thumbnail[:16]) or image.mime_type
            if mime in VISION_MIME_TYPES:
                inputs.append(TriageInput(index=index, data=image.thumbnail, mime_type=mime,
                                          camera_prior=image.camera_prior))
        stats: Dict[str, Any] = {"calls": 0, "cost": 0.0, "refused": 0, "images": 0}
        verdicts: _Verdicts = {}
        if inputs:
            agent = await self._agent(user_id)
            try:
                verdicts = await self._classify(agent, inputs, account_id, stats)
            except DriveModelUnavailable as exc:
                raise DriveModelUnavailable(str(exc), images_sent=stats["images"], model_calls=stats["calls"],
                                            model_cost=stats["cost"]) from exc
        results: List[TriageResult] = []
        unanswered = 0
        for index, image in enumerate(images, start=1):
            verdict, caption = verdicts.get(index, (TriageVerdict.PHOTO, ""))
            unanswered += index not in verdicts
            results.append(TriageResult(item_id=image.item_id, verdict=verdict, caption=caption))
        logger.info(f"[DriveTriage] user={user_id[:8]} images={len(images)} sent={len(inputs)} "
                    f"text={sum(r.verdict is TriageVerdict.TEXT for r in results)} unanswered={unanswered} "
                    f"refused={stats['refused']} calls={stats['calls']} cost=${stats['cost']:.4f}")
        return TriageBatchOutcome(results=tuple(results), model_calls=stats["calls"], model_cost=stats["cost"])

    async def _agent(self, user_id: str) -> DriveImageTriageAgent:
        user = await self._user_repo.get_user(user_id)
        if user is None:
            logger.error(f"[DriveTriage] user {user_id[:8]} not found")
            raise DriveModelUnavailable("user not found")
        try:
            context = self._context_builder.build(AGENT_TYPE, user.config)
        except Exception as exc:
            logger.error(f"[DriveTriage] cannot build triage for user {user_id[:8]}: {type(exc).__name__}: {exc}")
            raise DriveModelUnavailable("triage provider unavailable") from exc
        agent = DriveImageTriageAgent(
            AgentConfig(agent_id=f"{AGENT_TYPE}_{user_id}", agent_type=AGENT_TYPE, capabilities=[AGENT_TYPE]),
            context, self._prompt_builder, thinking=user.config.get_thinking_for_agent(AGENT_TYPE))
        # Mirror UserAgentFactory's post-construction wiring: without it _flush_billing is a silent no-op.
        agent._quota_service = self._quota_service
        return agent

    async def _classify(self, agent: DriveImageTriageAgent, inputs: List[TriageInput], account_id: str,
                        stats: Dict[str, Any]) -> _Verdicts:
        """One call; a refused batch is halved until the refused image stands alone (it is then left out). Calls and
        cost as the agent's ledger reports them, also for a failed call; images count once answered."""
        try:
            reply = await agent.classify(inputs, account_id=account_id)
        except (DriveModelCallFailed, DriveModelUnavailable) as exc:
            stats["calls"] += exc.model_calls
            stats["cost"] += exc.model_cost
            if isinstance(exc, DriveModelUnavailable):
                raise
            refused = exc
        else:
            stats["calls"] += reply.calls
            stats["cost"] += reply.cost
            stats["images"] += len(inputs)
            return dict(reply.verdicts)
        if len(inputs) == 1:
            logger.warning(f"[DriveTriage] image refused ({refused}); kept as a photograph without a caption")
            stats["refused"] += 1
            return {}
        mid = len(inputs) // 2
        left = await self._classify(agent, inputs[:mid], account_id, stats)
        right = await self._classify(agent, inputs[mid:], account_id, stats)
        return {**left, **right}
