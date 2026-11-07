"""
EmailClassifierRunner — the email classifier as an EmailClassifierPort, built per batch for the user whose mail it is
(docs/05_building_blocks/gmail_email_indexing/README.md).

Public interface:
    EmailClassifierRunner(context_builder, user_repo, prompt_builder, gmail=None, quota_service=None,
                          prompt_content_store=None)
        async classify_batch(emails, user_id, credentials=None, *, account_id) -> List[EmailClassificationResult]
            raises RuntimeError when the user is unknown or the classifier cannot be built (the indexing job fails
            and the page is retried; nothing is classified under another configuration)

Per batch it reads the user's UserBotConfig, builds the agent through AgentContextBuilder (tier and model are the
user's configuration for `email_classifier`), sets the quota service and the prompt-content store the way
UserAgentFactory does for its agents, and runs the batch with that user as the request context's user: the batch's
model calls are billed to `account_id` and recorded in `prompt_content` under the user and the account. The
DriveReaderRunner / CompanionExtractorRunner pattern: services/ cannot import agents/, so composition builds the
agent.
"""
from typing import Any, List, Optional

from ..agents.email_classification_agent import EmailClassificationAgent
from ..domain.agent import AgentConfig
from ..domain.email import EmailClassificationResult, EmailMetadata
from ..domain.oauth_credential import OAuthCredential
from ..ports.email_classifier_port import EmailClassifierPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.prompt_content_store import PromptContentStore
from ..ports.quota_service import QuotaService
from ..ports.user_repository import UserRepository
from ..utils.logger import logger
from ..utils.telemetry import request_user_scope

AGENT_TYPE = "email_classifier"


class EmailClassifierRunner(EmailClassifierPort):

    def __init__(
        self,
        context_builder: Any,
        user_repo: Optional[UserRepository],
        prompt_builder: PromptBuilderPort,
        gmail: Optional[EmailProviderPort] = None,
        quota_service: Optional[QuotaService] = None,
        prompt_content_store: Optional[PromptContentStore] = None,
    ) -> None:
        self._context_builder = context_builder
        self._user_repo = user_repo
        self._prompt_builder = prompt_builder
        self._gmail = gmail
        self._quota_service = quota_service
        self._prompt_content_store = prompt_content_store

    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        credentials: Optional[OAuthCredential] = None,
        *,
        account_id: str,
    ) -> List[EmailClassificationResult]:
        if not emails:
            return []
        agent = await self._agent(user_id)
        with request_user_scope(user_id):
            return await agent.classify_batch(emails, user_id, credentials, account_id=account_id)

    async def _agent(self, user_id: str) -> EmailClassificationAgent:
        """The classifier with this user's tier and model; RuntimeError when it cannot be built."""
        if self._user_repo is None:
            raise RuntimeError("email classifier: no user repository is wired")
        user = await self._user_repo.get_user(user_id)
        if user is None:
            logger.error(f"[EmailClassifier] user {user_id[:8]} not found")
            raise RuntimeError(f"email classifier: user {user_id[:8]} not found")
        try:
            context = self._context_builder.build(AGENT_TYPE, user.config)
        except Exception as exc:
            logger.error(f"[EmailClassifier] cannot build the classifier for user {user_id[:8]}: "
                         f"{type(exc).__name__}: {exc}")
            raise RuntimeError("email classifier: provider unavailable") from exc
        agent = EmailClassificationAgent(
            config=AgentConfig(agent_id=f"{AGENT_TYPE}_{user_id}", agent_type=AGENT_TYPE),
            execution_context=context,
            prompt_builder=self._prompt_builder,
            gmail=self._gmail,
            user_id=user_id,
        )
        # Mirror UserAgentFactory's post-construction wiring: without it _flush_billing and the content capture in
        # _call_llm are silent no-ops.
        agent._quota_service = self._quota_service
        agent._prompt_content_store = self._prompt_content_store
        return agent
