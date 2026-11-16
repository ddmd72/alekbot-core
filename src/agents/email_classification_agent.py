"""
Email Classification Agent — the email gate
===========================================

Decides keep or drop, with one closed reason, for every mail of a listed page (email index v2 §4). One shot: one
call in JSON mode, no tools; the gate sees the headers and the snippet only (§6.2). The closed reasons are code
constants (`src/domain/email_gate.py`) rendered into the prompt as a static block; code applies the lean-to-keep rule
over the answer (`gate_decisions`).

Prompt assembled via PromptBuilder (blueprint="email_classifier_v1").

Primary API: classify_batch() — called by EmailIndexingService through EmailClassifierRunner.
Not part of the conversational delegation chain.
"""

import json
from typing import List, Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.email import EmailClassificationResult, EmailMetadata
from ..domain.email_gate import gate_decisions, render_gate_reasons_block
from ..domain.exceptions import PROVIDER_OUTAGE_TYPES
from ..ports.llm_port import AgentExecutionContext, LLMRequest, LLMResponse, Message, MessagePart, PROMPT_CACHE_BOUNDARY
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from ..infrastructure.agent_config import EMAIL_CLASSIFICATION

MAX_PARSE_RETRIES = EMAIL_CLASSIFICATION.max_parse_retries

_SNIPPET_CHARS = 300


class EmailClassificationAgent(BaseAgent):
    """
    The email gate over one listed page.

    Not an EmailClassifierPort itself: the port's implementation is EmailClassifierRunner (composition), which
    builds this agent per batch for the job's user and passes the job's account.

    Output schema per listed mail: {email_id, decision: keep|drop, reason}.
    """

    MAX_TOKENS = EMAIL_CLASSIFICATION.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._user_id = user_id
        logger.info(
            f"📧 EmailClassificationAgent initialized "
            f"(tier={execution_context.tier.value}, model={self._model_name})"
        )

    # ------------------------------------------------------------------
    # BaseAgent contract (not used in delegation chain)
    # ------------------------------------------------------------------

    async def can_handle(self, message: AgentMessage) -> bool:
        return False

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error="EmailClassificationAgent is not part of the delegation chain. "
                  "Use classify_batch() directly.",
        )

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        *,
        account_id: str,
    ) -> List[EmailClassificationResult]:
        """
        One decision per input email, in input order. A failed call marks every email `failure` (never kept); a
        provider outage propagates (Cloud Tasks retries the page).
        account_id: the account whose mail this is. The batch is one execution: every model call of it goes into one
        TokenLedger billed to this account (flushed at the end, also on failure) and is recorded under it.
        """
        if not emails:
            return []
        with self._execution_billing_scope(account_id):
            try:
                return await self._classify(emails, user_id, account_id)
            finally:
                await self._flush_billing()

    async def _classify(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        account_id: str,
    ) -> List[EmailClassificationResult]:
        prompt = await self._build_prompt(user_id, account_id)
        emails_json = self._format_emails(emails)

        # Emails go before the cache boundary, after the static template (same pattern as ConsolidationAgent).
        if PROMPT_CACHE_BOUNDARY in prompt:
            static, dynamic = prompt.split(PROMPT_CACHE_BOUNDARY, 1)
            system_instruction = static + "\nEmails to classify:\n" + emails_json + PROMPT_CACHE_BOUNDARY + dynamic
        else:
            system_instruction = prompt + "\nEmails to classify:\n" + emails_json

        history: List[Message] = [
            Message(role="user", parts=[MessagePart(text=(
                f"Decide keep or drop, with its reason, for every mail listed above ({len(emails)} in all)."
            ))])
        ]

        try:
            for attempt in range(1, MAX_PARSE_RETRIES + 2):
                request = LLMRequest(
                    model_name=self._model_name,
                    system_instruction=system_instruction,
                    messages=history,
                    tools=None,
                    max_tokens=self.MAX_TOKENS,
                    response_mime_type="application/json",
                    disable_safety=True,
                    thinking="medium",
                )
                response: LLMResponse = await self._call_llm(request, turn=attempt)
                if response.usage_metadata:
                    u = response.usage_metadata
                    logger.info(
                        f"💰 classify_batch: attempt {attempt} tokens — "
                        f"in={u.prompt_tokens} out={u.completion_tokens} total={u.total_tokens}"
                    )
                raw = response.text or ""
                try:
                    return self._parse_response(raw, emails)
                except ValueError as parse_err:
                    logger.warning(
                        f"⚠️ classify_batch: invalid JSON (attempt {attempt}/{MAX_PARSE_RETRIES + 1}): {parse_err}"
                    )
                    history.append(Message(role="model", parts=[MessagePart(text=raw)]))
                    history.append(Message(role="user", parts=[MessagePart(text=(
                        f"Your previous output was not valid JSON. Error: {parse_err}. "
                        "Output ONLY the valid JSON array as required by output_format. "
                        "No markdown, no explanation. Start with [ and end with ]."
                    ))]))
            logger.error(f"💥 classify_batch: parse_error after {MAX_PARSE_RETRIES + 1} attempts")
            return self._all_failed(emails, "parse_error")

        except PROVIDER_OUTAGE_TYPES as exc:
            # The provider could not serve now (incl. both providers down) — let Cloud Tasks
            # retry the whole page instead of silently marking all emails as failed.
            logger.warning(
                f"⚠️ classify_batch transient error ({type(exc).__name__}): {exc} — "
                f"propagating for Cloud Tasks retry"
            )
            raise
        except Exception as exc:
            logger.error(f"💥 EmailClassificationAgent.classify_batch failed: {exc}")
            return self._all_failed(emails, type(exc).__name__)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _build_prompt(self, user_id: str, account_id: Optional[str] = None) -> str:
        """Return assembled prompt from PromptBuilder (Firestore) with the reasons block. Crashes if unavailable."""
        if not self._prompt_builder:
            raise RuntimeError(
                "EmailClassificationAgent requires prompt_builder — "
                "wire up PromptAssemblyService before calling classify_batch()"
            )
        return await self._prompt_builder.build_for_agent(
            agent_type="email_classifier",
            user_id=user_id or self._user_id,
            account_id=account_id,
            include_biographical=False,
            include_directives=False,
            include_location=False,
            extra_static_blocks=[render_gate_reasons_block()],
        )

    @staticmethod
    def _format_emails(emails: List[EmailMetadata]) -> str:
        items = []
        for e in emails:
            items.append({
                "email_id": e.email_id,
                "subject": e.subject,
                "from": e.from_address,
                "date": e.date.strftime("%Y-%m-%d") if e.date else "",
                "snippet": e.snippet[:_SNIPPET_CHARS] if e.snippet else "",
            })
        return json.dumps(items, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_response(raw: str, emails: List[EmailMetadata]) -> List[EmailClassificationResult]:
        """The gate's answer as one result per input email. Raises ValueError when the output is not a JSON array of
        objects; everything inside a well-formed array goes through the code rule (`gate_decisions`)."""
        try:
            items = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON decode error at char {exc.pos}: {exc.msg}") from exc
        if not isinstance(items, list):
            raise ValueError(f"Expected JSON array, got {type(items).__name__}")
        if not all(isinstance(item, dict) for item in items):
            raise ValueError("Expected every array item to be an object")

        decisions = gate_decisions(items, [e.email_id for e in emails])
        results = []
        for e in emails:
            decision = decisions[e.email_id]
            results.append(EmailClassificationResult(
                email_id=e.email_id, keep=decision.keep, reason=decision.reason, forced=decision.forced))
        return results

    @staticmethod
    def _all_failed(emails: List[EmailMetadata], failure: str) -> List[EmailClassificationResult]:
        return [EmailClassificationResult(email_id=e.email_id, keep=False, failure=failure) for e in emails]
