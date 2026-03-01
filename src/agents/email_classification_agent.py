"""
Email Classification Agent
==========================

Classifies a batch of email metadata using Gemini Flash with function calling.

Agentic flow (mirrors the validated POC):
  1. LLM receives all email metadata in one message.
  2. LLM calls get_email_details([ids]) for emails with ambiguous snippets.
  3. LLM produces final JSON array covering all emails.
  MAX_TURNS = 4 (matching POC).

Falls back to single-pass (no tool calling) when gmail/credentials are absent.

Prompt assembled via PromptBuilder (blueprint="email_classifier_v1").

Primary API: classify_batch() — called directly by EmailIndexingService.
Not part of the conversational delegation chain.
"""

import json
from typing import Any, Dict, List, Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.email import EmailClassificationResult, EmailMetadata, OAuthCredentials
from ..ports.email_classifier_port import EmailClassifierPort
from ..ports.email_provider_port import EmailProviderPort
from ..ports.llm_service import AgentExecutionContext, LLMRequest, LLMResponse, LLMService, Message, MessagePart, PROMPT_CACHE_BOUNDARY, ToolCall
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.debug_logger import get_debug_logger
from ..utils.logger import logger

MAX_TURNS = 4
MAX_PARSE_RETRIES = 1  # One LLM retry on invalid JSON before giving up


class EmailClassificationAgent(BaseAgent, EmailClassifierPort):
    """
    Classifies a batch of email metadata via Gemini Flash with function calling.

    When gmail + credentials are provided, uses a multi-turn agentic loop
    (MAX_TURNS=4) where the LLM can call get_email_details() for ambiguous emails.
    Falls back to single-pass classification when credentials are absent.

    Output schema per email:
      {email_id, valuable, valuable_type, category, fact, tags}
    """

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        gmail: Optional[EmailProviderPort] = None,
        user_id: Optional[str] = None,
    ):
        super().__init__(config)
        self._llm = execution_context.provider
        self._model_name = execution_context.model_name
        self._prompt_builder = prompt_builder
        self._gmail = gmail
        self._user_id = user_id
        logger.info(
            f"📧 EmailClassificationAgent initialized "
            f"(tier={execution_context.tier.value}, model={self._model_name}, tool_calling={'yes' if gmail else 'no'})"
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

    def _get_alternative_agents(self) -> List[str]:
        return []

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    async def classify_batch(
        self,
        emails: List[EmailMetadata],
        user_id: str,
        credentials: Optional[OAuthCredentials] = None,
    ) -> List[EmailClassificationResult]:
        """
        Classify up to 100 emails in an agentic tool-calling loop.
        Returns one result per input email (including valuable=False).

        The LLM calls get_email_details() for uncertain emails to inspect
        body text and attachment filenames before deciding.
        credentials: required for get_email_details tool calls.
        """
        if not emails:
            return []

        prompt = await self._build_prompt(user_id)
        emails_json = self._format_emails(emails)

        # Embed emails into system instruction before the cache boundary so they
        # are part of the cached static prefix (same pattern as ConsolidationAgent).
        if PROMPT_CACHE_BOUNDARY in prompt:
            static, dynamic = prompt.split(PROMPT_CACHE_BOUNDARY, 1)
            system_instruction = static + "\nEmails to classify:\n" + emails_json + PROMPT_CACHE_BOUNDARY + dynamic
        else:
            system_instruction = prompt + "\nEmails to classify:\n" + emails_json

        has_tools = bool(self._gmail and credentials)
        tool_declarations = self._get_tool_declarations() if has_tools else None

        history: List[Message] = [
            Message(role="user", parts=[MessagePart(text=(
                f"The following {len(emails)} emails are candidates for the user's long-term personal memory. "
                "Most will not qualify — your job is to filter ruthlessly and surface only those "
                "that represent a confirmed fact with lasting personal significance."
            ))])
        ]

        logger.debug(
            f"classify_batch: {len(emails)} emails | "
            f"tool_calling={'yes' if has_tools else 'no'} | "
            f"system_instruction ({len(system_instruction)} chars):\n{system_instruction}"
        )

        debug_logger = get_debug_logger()
        debug_logger.log_prompt(
            agent_name="email_classification",
            prompt=emails_json,
            metadata={"model": self._model_name, "emails": len(emails), "tool_calling": has_tools},
            system_instruction=system_instruction,
        )

        parse_retries = 0

        try:
            for turn in range(1, MAX_TURNS + 1):
                request = LLMRequest(
                    model_name=self._model_name,
                    system_instruction=system_instruction,
                    messages=history,
                    tools=tool_declarations,
                    temperature=0.0,
                    max_tokens=65535,
                    response_mime_type="application/json" if not has_tools else None,
                    disable_safety=True,
                    enable_reasoning=True,
                )

                logger.debug(f"classify_batch: turn {turn}/{MAX_TURNS}")
                response: LLMResponse = await self._llm.generate_content(request=request)
                if response.usage_metadata:
                    u = response.usage_metadata
                    logger.info(
                        f"💰 classify_batch: turn {turn} tokens — "
                        f"in={u.prompt_tokens} out={u.completion_tokens} total={u.total_tokens}"
                    )

                if not response.tool_calls:
                    # Final turn — LLM should produce the JSON classification array
                    raw = response.text or ""
                    logger.debug(
                        f"classify_batch: final response turn {turn} ({len(raw)} chars):\n{raw}"
                    )
                    debug_logger.log_response(
                        agent_name="email_classification",
                        response=raw,
                        metadata={
                            "model": self._model_name,
                            "turn": turn,
                            "emails": len(emails),
                            **({"tokens": {"in": response.usage_metadata.prompt_tokens, "out": response.usage_metadata.completion_tokens}} if response.usage_metadata else {}),
                        },
                    )
                    try:
                        results = self._parse_response(raw, emails)
                    except ValueError as parse_err:
                        parse_retries += 1
                        logger.warning(
                            f"⚠️ classify_batch: invalid JSON on turn {turn} "
                            f"(attempt {parse_retries}/{MAX_PARSE_RETRIES + 1}): {parse_err}\n"
                            f"  Raw ({len(raw)} chars): {raw[:300]}"
                        )
                        if parse_retries > MAX_PARSE_RETRIES:
                            logger.error(
                                f"💥 classify_batch: parse_error after {parse_retries} attempts"
                            )
                            return self._all_failed(emails)
                        # Retry: send bad response + correction back to the LLM
                        history.append(
                            Message(role="model", parts=[MessagePart(text=raw)])
                        )
                        history.append(
                            Message(
                                role="user",
                                parts=[MessagePart(text=(
                                    f"Your previous output was not valid JSON. Error: {parse_err}. "
                                    "Output ONLY the valid JSON array as required by output_format. "
                                    "No markdown, no explanation. Start with [ and end with ]."
                                ))],
                            )
                        )
                        continue

                    valuable = [r for r in results if r.valuable]
                    logger.info(
                        f"📊 classify_batch: {len(emails)} emails → {len(valuable)} valuable "
                        f"(turns={turn})"
                    )
                    for r in valuable:
                        logger.debug(
                            f"  ✅ {r.email_id} | {r.category} | {r.fact} | tags={r.tags}"
                        )
                    for r in results:
                        if not r.valuable:
                            logger.debug(f"  ❌ {r.email_id}")
                    return results

                # LLM called get_email_details — execute and continue
                logger.info(
                    f"classify_batch: turn {turn} — "
                    f"{len(response.tool_calls)} tool call(s)"
                )

                # Append model's tool-call turn to history
                if response.raw_content is not None:
                    history.append(
                        Message(role="model", parts=[], raw_content=response.raw_content)
                    )
                else:
                    history.append(
                        Message(
                            role="model",
                            parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
                        )
                    )

                # Execute each tool call and collect tool responses
                tool_response_parts: List[MessagePart] = []
                for tc in response.tool_calls:
                    if tc.name != "get_email_details":
                        logger.warning(
                            f"classify_batch: unexpected tool '{tc.name}' — skipping"
                        )
                        continue

                    requested_ids: List[str] = tc.args.get("email_ids", [])
                    logger.info(
                        f"  → get_email_details for {len(requested_ids)} email(s): "
                        f"{[eid[:8] for eid in requested_ids]}"
                    )

                    details = await self._fetch_email_details(requested_ids, credentials)
                    tool_response_parts.append(
                        MessagePart(
                            tool_response={
                                "name": "get_email_details",
                                "response": {"result": details},
                            }
                        )
                    )

                history.append(Message(role="user", parts=tool_response_parts))

            # MAX_TURNS reached without a final response
            logger.warning(
                f"⚠️ classify_batch: MAX_TURNS={MAX_TURNS} reached without final classification"
            )
            return self._all_failed(emails)

        except Exception as exc:
            logger.error(f"💥 EmailClassificationAgent.classify_batch failed: {exc}")
            return self._all_failed(emails)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _build_prompt(self, user_id: str) -> str:
        """Return assembled prompt from PromptBuilder (Firestore). Crashes if unavailable."""
        if not self._prompt_builder:
            raise RuntimeError(
                "EmailClassificationAgent requires prompt_builder — "
                "wire up PromptAssemblyService before calling classify_batch()"
            )
        prompt = await self._prompt_builder.build_for_agent(
            agent_type="email_classifier",
            user_id=user_id or self._user_id,
            account_id=None,
            include_biographical=False,
        )
        logger.debug("classify_batch: prompt assembled from Firestore")
        return prompt

    async def _fetch_email_details(
        self,
        email_ids: List[str],
        credentials: Optional[OAuthCredentials],
    ) -> List[Dict[str, Any]]:
        """Fetch full body + attachment filenames for the given email IDs."""
        if not self._gmail or not credentials or not email_ids:
            return [{"email_id": eid, "body": "", "attachments": []} for eid in email_ids]

        try:
            content_map = await self._gmail.batch_get_full_content(
                credentials=credentials,
                email_ids=email_ids,
                deep=False,
            )
        except Exception as exc:
            logger.warning(f"⚠️ get_email_details fetch failed: {exc}")
            return [{"email_id": eid, "body": "", "attachments": []} for eid in email_ids]

        details = []
        for eid in email_ids:
            content = content_map.get(eid)
            details.append({
                "email_id": eid,
                "body": (content.body_text or "")[:3000] if content else "",
                "attachments": content.attachments if content else [],
            })
        return details

    @staticmethod
    def _get_tool_declarations() -> List[Dict[str, Any]]:
        return [
            {
                "name": "get_email_details",
                "description": (
                    "Fetch full body text and attachment filenames for emails that need "
                    "deeper analysis. Use when snippet is empty or too short, or when "
                    "attachments are the key signal."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "email_id values to fetch full details for",
                        }
                    },
                    "required": ["email_ids"],
                },
            }
        ]

    @staticmethod
    def _format_emails(emails: List[EmailMetadata]) -> str:
        items = []
        for e in emails:
            items.append({
                "email_id": e.email_id,
                "subject": e.subject,
                "from": e.from_address,
                "date": e.date.strftime("%Y-%m-%d") if e.date else "",
                "snippet": e.snippet[:300] if e.snippet else "",
            })
        return json.dumps(items, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse_response(
        raw: str, emails: List[EmailMetadata]
    ) -> List[EmailClassificationResult]:
        """Parse LLM JSON array output. Raises ValueError if output is not a valid JSON array."""
        text = raw.strip()
        # Extract JSON from inside a markdown code block (with optional preamble text).
        if "```" in text:
            start = text.index("```")
            end = text.rfind("```")
            if end > start:
                block = text[start:end].splitlines()
                text = "\n".join(block[1:]).strip()  # drop the opening ```[json] line
        try:
            items = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON decode error at char {exc.pos}: {exc.msg}") from exc

        if not isinstance(items, list):
            raise ValueError(f"Expected JSON array, got {type(items).__name__}")

        expected_ids = {e.email_id for e in emails}
        results_by_id: dict = {}

        for item in items:
            email_id = item.get("email_id", "")
            if email_id not in expected_ids:
                continue
            results_by_id[email_id] = EmailClassificationResult(
                email_id=email_id,
                valuable=bool(item.get("valuable", False)),
                category=item.get("category") or None,
                fact=item.get("fact") or None,
                tags=[t.lower() for t in (item.get("tags") or [])],
                valuable_type=item.get("valuable_type", "confirmed_event"),
            )

        results = []
        for e in emails:
            if e.email_id in results_by_id:
                results.append(results_by_id[e.email_id])
            else:
                results.append(
                    EmailClassificationResult(
                        email_id=e.email_id,
                        valuable=False,
                        category=None,
                        fact=None,
                        tags=[],
                    )
                )

        return results

    @staticmethod
    def _all_failed(
        emails: List[EmailMetadata],
    ) -> List[EmailClassificationResult]:
        return [
            EmailClassificationResult(
                email_id=e.email_id,
                valuable=False,
                category=None,
                fact=None,
                tags=[],
            )
            for e in emails
        ]
