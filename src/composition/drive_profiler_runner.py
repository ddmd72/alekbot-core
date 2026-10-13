"""
DriveProfilerRunner — the profiler agent as a DocumentProfilerPort, on the indexer
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.4.1, §25.4.4, §25.4.6).

Public interface:
    DriveProfilerRunner(context_builder, user_repo, prompt_builder, quota_service=None)
        async profile(request) -> ProfileReply                never raises

Per call it builds the agent from the user's configuration (provider, tier, thinking; D7) and bills to
`request.account_id` (the DriveReaderRunner pattern; no prompt-content store: the file's text stays out of BigQuery).
Key values are asked for only where `drive_profile.PROFILE_POLICY` gives the kind a full profile. Outcomes
(`ModelReplyStatus`, §25.4.6):
- ok: the model's profile, unverified (the caller runs `drive_profile.verify`);
- stopped_at_cap: the reply stopped at the output cap, asked once more with halved list caps, stopped again;
- refused (the primary refused the input, or a safety stop), invalid (output still invalid after the parse retry),
  failed (a 5xx, a timeout, an unexpected error, the fallback's refusal — after a call), unavailable (429, network,
  breaker, account 4xx, missing key or prompt, no user: nothing charged).
Every reply carries the model that served, the whole cost and the fallback's share, already billed.
"""
from __future__ import annotations

from typing import Any, Optional

from ..agents.drive_profiler_agent import AGENT_TYPE, FULL_CAPS, DriveProfilerAgent
from ..domain.drive_enrichment_calls import ModelReplyStatus, ProfileReply, ProfileRequest
from ..domain.drive_extraction import DriveModelUnavailable
from ..domain.drive_profile import PROFILE_POLICY, ProfileOutput
from ..infrastructure.drive_model_call import DriveModelCallFailed, reply_status
from ..ports.document_profiler_port import DocumentProfilerPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger
from .drive_agent_build import build_drive_agent


class DriveProfilerRunner(DocumentProfilerPort):

    def __init__(self, context_builder: Any, user_repo: UserRepository, prompt_builder: PromptBuilderPort,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._quota_service = quota_service

    async def profile(self, request: ProfileRequest) -> ProfileReply:
        reply = await self._profile(request)
        logger.info(f"[DriveProfiler] user={request.user_id[:8]} item={request.item_id} kind={request.kind} "
                    f"status={reply.status.value} model={reply.model or '-'} cost=${reply.usd:.5f} "
                    f"fallback=${reply.fallback_usd:.5f}")
        return reply

    async def _profile(self, request: ProfileRequest) -> ProfileReply:
        try:
            agent = await build_drive_agent(DriveProfilerAgent, AGENT_TYPE, request.user_id,
                                            context_builder=self._context_builder, user_repo=self._user_repo,
                                            prompt_builder=self._prompt_builder, quota_service=self._quota_service)
        except DriveModelUnavailable:
            return ProfileReply(status=ModelReplyStatus.UNAVAILABLE, profile=None, model="", usd=0.0)
        policy = PROFILE_POLICY.get(request.kind)
        key_values = policy is not None and policy.output is ProfileOutput.FULL
        usd = fallback_usd = 0.0
        model = ""
        caps = FULL_CAPS
        for _ in range(2):   # the first answer, and once more with halved caps when it stopped at the cap (§25.4.4)
            try:
                answer = await agent.profile(request, key_values=key_values, caps=caps)
            except (DriveModelCallFailed, DriveModelUnavailable) as exc:
                usd += getattr(exc, "model_cost", 0.0)
                fallback_usd += getattr(exc, "model_fallback_cost", 0.0)
                model = getattr(exc, "model_name", "") or model
                return ProfileReply(status=reply_status(exc), profile=None, model=model, usd=round(usd, 6),
                                    fallback_usd=round(fallback_usd, 6))
            except Exception as exc:   # never raise across the port: an unexpected error is a charged failure
                logger.error(f"[DriveProfiler] unexpected failure: {type(exc).__name__}: {exc}", exc_info=exc)
                return ProfileReply(status=ModelReplyStatus.FAILED, profile=None, model=model, usd=round(usd, 6),
                                    fallback_usd=round(fallback_usd, 6))
            usd += answer.usd
            fallback_usd += answer.fallback_usd
            model = answer.model
            if not answer.output_limited:
                return ProfileReply(status=ModelReplyStatus.OK, profile=answer.profile, model=model,
                                    usd=round(usd, 6), fallback_usd=round(fallback_usd, 6))
            caps = caps.halved()
        logger.warning(f"[DriveProfiler] item {request.item_id}: still at the output cap with halved list caps")
        return ProfileReply(status=ModelReplyStatus.STOPPED_AT_CAP, profile=None, model=model, usd=round(usd, 6),
                            fallback_usd=round(fallback_usd, 6))
