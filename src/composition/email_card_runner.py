"""
EmailCardRunner — the email card writer as an EmailCardWriterPort (email index v2, slice 3).

Public interface:
    card_writer_model(raw) -> str      the pinned model: the stripped `EMAIL_CARD_MODEL` value, else the default
    EmailCardRunner(context_builder, prompt_builder, model, quota_service=None)
        async write(account_id, static_block, card_input_text) -> CardWriterReply   never raises

The agent is built once, lazily, with a default user configuration (the card writer is the system's, not a user's) and
the pinned model. services/ cannot import agents/, so composition builds the agent. The knob is read where the container
or script is built, never in `agents/` (REQ-ARCH-17) and never in `load_settings()` (an optional knob). The runner passes
no prompt-content store: mail bodies and document text are never stored (RFC §1), and BigQuery would hold them for 30 days.
"""
import asyncio
from typing import Any, Optional

from ..agents.email_card_agent import AGENT_TYPE, EmailCardAgent
from ..domain.agent import AgentConfig
from ..domain.email_card_reply import CardWriterReply
from ..domain.user import UserBotConfig
from ..infrastructure.agent_config import EMAIL_CARD
from ..ports.email_card_writer_port import EmailCardWriterPort
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.quota_service import QuotaService
from ..utils.logger import logger


def card_writer_model(raw: Optional[str]) -> str:
    """The model the card writer runs on: `raw` (the `EMAIL_CARD_MODEL` value) stripped, else the default."""
    value = (raw or "").strip()
    return value or EMAIL_CARD.default_model


class EmailCardRunner(EmailCardWriterPort):

    def __init__(self, context_builder: Any, prompt_builder: PromptBuilderPort, model: str,
                 quota_service: Optional[QuotaService] = None) -> None:
        self._context_builder = context_builder
        self._prompt_builder = prompt_builder
        self._model = model
        self._quota_service = quota_service
        self._agent: Optional[EmailCardAgent] = None
        self._lock = asyncio.Lock()

    async def write(self, account_id: str, static_block: str, card_input_text: str) -> CardWriterReply:
        agent = await self._get_agent()
        if agent is None:
            return CardWriterReply.unavailable(self._model)
        return await agent.write(account_id, static_block, card_input_text)

    async def _get_agent(self) -> Optional[EmailCardAgent]:
        async with self._lock:
            if self._agent is None:
                try:
                    self._agent = self._build()
                except Exception as exc:
                    logger.error(f"[EmailCard] cannot build the card writer: {type(exc).__name__}: {exc}")
                    return None
            return self._agent

    def _build(self) -> EmailCardAgent:
        context = self._context_builder.build(AGENT_TYPE, UserBotConfig())
        context = context.model_copy(update={"model_name": self._model})
        agent = EmailCardAgent(
            config=AgentConfig(agent_id=AGENT_TYPE, agent_type=AGENT_TYPE),
            execution_context=context,
            prompt_builder=self._prompt_builder,
        )
        # Mirror UserAgentFactory's post-construction wiring: without it _flush_billing is a silent no-op.
        agent._quota_service = self._quota_service
        return agent
