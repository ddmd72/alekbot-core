"""
TutorAgent — the text language tutor (RFC docs/10_rfcs/COMPANION_AGENTS_RFC.md §7/§9:
the pilot companion). Bound-channel-only, internal=True, not reachable from normal
conversation — a channel must be explicitly bound via "$agent tutor".

Structural mirror of DomainResearcherAgent (src/agents/domain_researcher_agent.py),
the established template for a bound-channel conversational specialist: reads
history from message.context["history"] (platform API, not SessionStore — bound
channels are stateless today, see ConversationHandler._resolve_session_mode),
uses DelegationEngine for its own tool-calling loop (search_web only — search_memory was
considered and explicitly declined: it reaches Alek's full personal biography, bypassing
the include_biographical=False permission boundary the companion-context assembler
otherwise enforces; see fix wave 2026-08-29).

The one new piece: injects CompanionContextAssemblerService's read-side output
(this session's own companion records + cached summary) as a static prompt block,
the same extra_static_blocks mechanism SmartResponseAgent already uses for
email_for_triage. Read toggles are the RFC §5 default-closed permission set:
include_biographical=False, include_standing_directives=False, no user_id passed —
own_records only. own_records will be empty until Phase F wires the write side
(overflow_callback branching by companion_config); reading now is zero-risk and
avoids a second "wire it later" stub, matching Phase A-D's own precedent.

The retrieval phrase fed to the assembler is cleaned of AgentCoordinator's
delegation timestamp prefix (``handle_delegation`` prepends
``[Mon DD, HH:MM UTC] `` to every delegated query — see ``_strip_delegation_timestamp``)
so the embedded vector is the user's actual utterance, not noise that changes
every minute.
"""
import json
import re
from typing import TYPE_CHECKING, List, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import TUTOR
from ..infrastructure.agent_manifest import TUTOR as TUTOR_DESCRIPTOR
from ..infrastructure.delegation_engine import DelegationEngine
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from .base_agent import BaseAgent
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.companion_context_assembler_service import (
        CompanionContextAssemblerService,
    )


# AgentCoordinator.handle_delegation() prepends this exact format to every
# delegated query (`ts = datetime.now(timezone.utc).strftime("[%b %d, %H:%M UTC]")`,
# agent_coordinator.py:426) — the timezone is always the literal "UTC", never the
# caller's local tz. Stripped before building retrieval phrases so the embedded
# vector is semantic content, not a timestamp that changes every minute.
_DELEGATION_TIMESTAMP_PREFIX = re.compile(r"^\[[A-Za-z]{3} \d{2}, \d{2}:\d{2} UTC\] ")


def _strip_delegation_timestamp(text: str) -> str:
    return _DELEGATION_TIMESTAMP_PREFIX.sub("", text, count=1)


class TutorAgent(BaseAgent):
    """Text language tutor — bound-channel conversational companion."""

    _descriptor = TUTOR_DESCRIPTOR

    TEMPERATURE = TUTOR.temperature

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: Optional[PromptBuilderPort] = None,
        assembler: Optional["CompanionContextAssemblerService"] = None,
        user_id: Optional[str] = None,
        user_timezone: str = "UTC",
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._assembler = assembler
        self.user_id = user_id
        self._user_timezone = user_timezone

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        if not query:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
            )
        return await self._converse(message, query)

    async def _converse(self, message: AgentMessage, query: str) -> AgentResponse:
        self._on_agent_start(query)

        extra_static_blocks = await self._build_companion_context_block(message, query)

        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="tutor",
                user_id=self.user_id,
                account_id=message.context.get("account_id"),
                routing_metadata=None,
                include_biographical=False,
                include_directives=False,
                include_datetime=False,
                extra_static_blocks=extra_static_blocks,
                kb_preamble=True,
            )
        except Exception as exc:
            self._on_agent_error(exc, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"PromptBuilder failed: {exc}",
            )

        # Build conversation history from bound channel context (platform API,
        # not SessionStore — mirrors DomainResearcherAgent exactly).
        history_data = message.context.get("history", [])
        messages = []
        for entry in history_data:
            role = entry.get("role", "user")
            parts_data = entry.get("parts", [])
            parts = [MessagePart(text=p.get("text", "")) for p in parts_data if p.get("text")]
            if parts:
                kwargs = {}
                if "created_at" in entry:
                    kwargs["created_at"] = entry["created_at"]
                messages.append(Message(role=role, parts=parts, **kwargs))

        current_parts = message.context.get("current_message_parts", [])
        if current_parts:
            messages.append(Message(role="user", parts=current_parts))
        else:
            messages.append(Message(role="user", parts=[MessagePart(text=query)]))

        messages = self._inject_timestamps(messages)

        try:
            tools = None
            if self.coordinator:
                available = self.coordinator.get_available_intents_for(self._descriptor)
                if available:
                    tools = [self._build_delegate_tool_declaration(available)]

            base_request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                tools=tools,
                temperature=self.TEMPERATURE,
                max_tokens=TUTOR.max_tokens,
            )

            if tools and self.coordinator:
                engine = DelegationEngine(self.coordinator)
                result = await engine.execute(
                    call_llm=self._call_llm,
                    base_request=base_request,
                    context=message.context,
                    max_turns=TUTOR.max_delegation_turns,
                    calling_agent_id=self.agent_id,
                )
                if result.failed:
                    return AgentResponse.failure(
                        task_id=message.task_id,
                        agent_id=self.agent_id,
                        error="max_turns_exhausted",
                    )
                result_text = result.text or "No response from model."
                token_count = result.total_tokens
            else:
                response = await self._call_llm(base_request)
                result_text = response.text or "No response from model."
                token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0

            self._on_agent_success(len(result_text), token_count, output_text=result_text)

            return AgentResponse.success(
                task_id=message.task_id,
                agent_id=self.agent_id,
                result=result_text,
            )
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(e),
            )

    async def _build_companion_context_block(
        self, message: AgentMessage, query: str,
    ) -> Optional[List[str]]:
        """Fetch this session's companion context and format it as a named static
        block for the prompt. Degrades to no block on any failure — companion
        context is enrichment, not a hard requirement for the tutor to respond."""
        if self._assembler is None:
            return None

        platform = message.context.get("origin_platform")
        channel_id = message.context.get("origin_channel_id")
        account_id = message.context.get("account_id")
        if not (platform and channel_id and account_id):
            logger.warning(
                "⚠️ [TutorAgent] companion context skipped — missing context "
                "(origin_platform=%s origin_channel_id=%s account_id=%s)",
                platform, channel_id, account_id,
            )
            return None
        session_id = f"{platform}:{channel_id}"

        # Clean the retrieval phrase of the coordinator's delegation timestamp
        # prefix (Important #1, review 2026-08-29) — the raw `query` is fine
        # everywhere else it's used (_on_agent_start, the fallback user
        # message); only the embedded phrase needs to be free of noise.
        clean_query = _strip_delegation_timestamp(query)

        try:
            companion_context = await self._assembler.assemble_context(
                session_id=session_id,
                account_id=account_id,
                query_phrases=[clean_query],
                include_own_records=True,
                include_biographical=False,
                include_standing_directives=False,
                user_id=None,
            )
        except Exception as exc:
            logger.warning("⚠️ [TutorAgent] companion context fetch failed: %s", exc)
            return None

        if not companion_context.session_summary and not companion_context.own_records:
            return None

        # DEFERRED (review 2026-08-29): own_records is query-dependent (changes every
        # turn) but rides in this STATIC block alongside session_summary, ahead of the
        # cache boundary — will hurt prompt-cache hit rate once Phase F starts writing
        # real records. Zero live impact today (own_records is always empty pre-Phase-F).
        # Fix means splitting it into query_specific_context; deliberately out of scope
        # for this fix wave.
        payload = {
            "session_summary": companion_context.session_summary or "",
            "own_records": [r.text for r in companion_context.own_records],
        }
        return [
            "companion_context {\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n}"
        ]

    def _get_alternative_agents(self) -> list[str]:
        return []
