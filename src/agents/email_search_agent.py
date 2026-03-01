"""
Email Search Agent
==================

Specialist agent for the user's indexed email archive.
Three intents, all routed through one agent instance:

  search_emails           — semantic search via 7-stream multi-vector RRF.
                            payload: {"query": "..."}
                            Requires LLM (key extraction) + embedding + Firestore.

  get_email_details       — fetch full Gmail body for a known email_id.
                            payload: {"email_id": "..."}
                            No LLM — direct Gmail API call via EmailSearchService.

  get_email_attachment    — download + convert attachment to text.
                            payload: {"email_id": "...", "filename": "file.pdf"}
                            No LLM — Gmail API + markitdown via EmailSearchService.

Routing: execute() dispatches on payload keys:
  email_id + filename  →  get_attachment
  email_id only        →  get_details
  query only           →  search_emails
"""

import json
import time
from typing import List, Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..ports.llm_service import (
    AgentExecutionContext,
    LLMRequest,
    Message,
    MessagePart,
)
from ..ports.prompt_builder_port import PromptBuilderPort
from ..services.email_search_service import EmailSearchService
from ..utils.debug_logger import get_debug_logger
from ..utils.logger import logger


class EmailSearchAgent(BaseAgent):
    """
    Specialist agent for searching the user's email archive.

    Uses LLM to extract semantic search parameters from the user request,
    then delegates 7-stream multi-vector RRF search to EmailSearchService.
    """

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        email_search_service: EmailSearchService,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._search_service = email_search_service
        self.user_id = user_id

        logger.info(
            f"📬 EmailSearchAgent initialized "
            f"(model={self.model_name}, user={user_id[:8] if user_id else 'NONE'})"
        )

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        payload = message.payload
        return bool(payload.get("email_id") or payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """Route to the appropriate handler based on payload keys."""
        payload = message.payload
        user_id = message.context.get("user_id") or self.user_id

        email_id: str = payload.get("email_id", "")
        filename: str = payload.get("filename", "")

        if email_id and filename:
            return await self._handle_get_attachment(message, email_id, filename, user_id)
        elif email_id:
            return await self._handle_get_details(message, email_id, user_id)
        else:
            return await self._handle_search_emails(message, user_id)

    # ------------------------------------------------------------------
    # Intent: get_email_details
    # ------------------------------------------------------------------

    async def _handle_get_details(
        self, message: AgentMessage, email_id: str, user_id: Optional[str]
    ) -> AgentResponse:
        """Fetch full body of a Gmail message. No LLM involved."""
        logger.info(
            f"📬 EmailSearchAgent.get_details: {email_id} "
            f"user={user_id[:8] if user_id else '?'}"
        )
        start_time = time.time()

        try:
            result = await self._search_service.get_details(
                email_id=email_id,
                user_id=user_id or "",
            )
        except Exception as exc:
            logger.error(f"📬 EmailSearchAgent: get_details failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"get_email_details failed: {exc}",
            )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent.get_details done in {duration_ms}ms")

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={"email_id": email_id, "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: get_email_attachment
    # ------------------------------------------------------------------

    async def _handle_get_attachment(
        self,
        message: AgentMessage,
        email_id: str,
        filename: str,
        user_id: Optional[str],
    ) -> AgentResponse:
        """Download + convert a Gmail attachment to text. No LLM involved."""
        logger.info(
            f"📬 EmailSearchAgent.get_attachment: {email_id}/{filename} "
            f"user={user_id[:8] if user_id else '?'}"
        )
        start_time = time.time()

        try:
            result = await self._search_service.get_attachment(
                email_id=email_id,
                filename=filename,
                user_id=user_id or "",
            )
        except Exception as exc:
            logger.error(f"📬 EmailSearchAgent: get_attachment failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"get_email_attachment failed: {exc}",
            )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent.get_attachment done in {duration_ms}ms")

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={"email_id": email_id, "filename": filename, "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: search_emails
    # ------------------------------------------------------------------

    async def _handle_search_emails(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        """LLM key extraction + 7-stream multi-vector RRF search."""
        query = message.payload.get("query", "")
        account_id = message.context.get("account_id")
        history: List[dict] = message.payload.get("history", [])

        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided",
            )

        logger.info(
            f"📬 EmailSearchAgent.search_emails: '{query[:60]}' "
            f"user={user_id[:8] if user_id else '?'} history_turns={len(history)}"
        )
        start_time = time.time()

        # Step 1: LLM extracts search parameters
        keys = await self._extract_search_queries(
            query=query,
            history=history,
            user_id=user_id,
            account_id=account_id,
        )
        primary_query = keys.get("primary_query") or query
        alternative_query = keys.get("alternative_query") or query
        tags: List[str] = keys.get("tags") or []

        logger.info(
            f"📬 EmailSearchAgent: primary='{primary_query[:50]}' "
            f"alt='{alternative_query[:50]}' tags={tags}"
        )

        # Step 2: 7-stream multi-vector RRF search
        try:
            result = await self._search_service.vector_search(
                primary_query=primary_query,
                alternative_query=alternative_query,
                tags=tags,
                user_id=user_id,
            )
        except Exception as exc:
            logger.error(f"📬 EmailSearchAgent: vector_search failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Email search failed: {exc}",
            )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(f"📬 EmailSearchAgent done in {duration_ms}ms")

        get_debug_logger().log_response(
            agent_name="email_search_to_smart",
            response=result,
            metadata={
                "user_id": (user_id or "")[:8],
                "duration_ms": duration_ms,
                "primary_query": primary_query,
                "alternative_query": alternative_query,
                "tags": tags,
            }
        )

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={
                "duration_ms": duration_ms,
                "primary_query": primary_query,
                "alternative_query": alternative_query,
                "tags": tags,
            },
        )

    # ------------------------------------------------------------------
    # Query extraction (used by search_emails only)
    # ------------------------------------------------------------------

    async def _extract_search_queries(
        self,
        query: str,
        history: List[dict],
        user_id: Optional[str],
        account_id: Optional[str],
    ) -> dict:
        """
        Use LLM to extract primary_query, alternative_query, and tags for vector search.
        Includes biographical context and optional conversation history.
        Falls back to empty dict (caller uses raw query) on any error.
        """
        # Build system prompt (separate try so failures don't skip debug logging)
        system_prompt = ""
        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="email_search",
                user_id=user_id,
                account_id=account_id,
                routing_metadata=None,
                include_biographical=True,
            )
        except Exception as exc:
            logger.warning(f"📬 EmailSearchAgent: build_for_agent failed ({exc}), proceeding with empty prompt")

        messages: List[Message] = []

        # Inject last 3 conversation turns as context (if caller provides them)
        for turn in history[-3:]:
            role = turn.get("role", "user")
            text = turn.get("text", "")
            if text:
                messages.append(
                    Message(role=role, parts=[MessagePart(text=text)])
                )

        user_text = f'EMAIL_SEARCH_REQUEST "{query}"'
        messages.append(
            Message(
                role="user",
                parts=[MessagePart(text=user_text)],
            )
        )

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=messages,
            tools=[],
            temperature=0.0,
            max_tokens=250,
            disable_safety=True,
            response_mime_type="application/json",
        )

        debug_logger = get_debug_logger()
        debug_logger.log_prompt(
            agent_name="email_search",
            prompt=user_text,
            system_instruction=system_prompt,
            metadata={"user_id": (user_id or "")[:8], "query": query[:80]}
        )

        try:
            response = await self._llm.generate_content(request=request)
            raw = (response.text or "").strip()
            logger.debug(f"📬 EmailSearchAgent LLM raw: {raw[:300]}")

            debug_logger.log_response(
                agent_name="email_search",
                response=raw,
                metadata={"user_id": (user_id or "")[:8]}
            )
            return json.loads(raw)

        except Exception as exc:
            logger.warning(
                f"📬 EmailSearchAgent: query extraction failed ({exc}), using raw query"
            )
            return {}
