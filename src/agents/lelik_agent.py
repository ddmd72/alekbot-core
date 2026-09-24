"""
LelikAgent — the voice companion (docs/10_rfcs/VOICE_COMPANION_RFC.md §4.4, §4.7).

Built the way TutorAgent is: its own prompt over context LelikPersonaService assembles,
and the shared delegate_to_specialist tool over LELIK.allowed_intents. Its LLM call is
a realtime session in the relay process, so there are three entry points, not one:
execute() places the callback (§4.6), session_config() feeds the session at pickup
(§4.8), delegate() runs one tool call from the live session through DelegationEngine.

execute() does not catch originate_call failures: the webhook's own try/except releases
the ticket and one-call marker.

delegate() also carries rule 2 (VOICE_COMPANION_RFC §4.10) generalized to every delegation,
not just ask_alek: any specialist's result may contain reading-shaped links (search_web
findings, bare URLs in prose), and those reach chat the same way Alek's own answer does.
"""
from typing import TYPE_CHECKING, Any, Dict, List
from urllib.parse import urlencode
from uuid import uuid4

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.llm import ToolCall
from ..domain.messaging import SmartResponse
from ..domain.result_links import extract_result_links
from ..infrastructure.agent_manifest import LELIK, Intent
from ..infrastructure.delegation_engine import DelegationEngine, normalize_delegate_context
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.telephony_port import TelephonyPort
from ..utils.logger import logger
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..services.lelik_persona_service import LelikPersonaService
    from ..services.user_notification_service import UserNotificationService

_DELEGATE_TOOL = "delegate_to_specialist"


class LelikAgent(BaseAgent):
    """The voice companion: places the call, feeds the session, delegates for it."""

    _descriptor = LELIK

    def __init__(
        self,
        config: AgentConfig,
        telephony: TelephonyPort,
        from_number: str,
        status_callback_url: str,
        prompt_builder: PromptBuilderPort,
        persona: "LelikPersonaService",
        notifications: "UserNotificationService",
    ) -> None:
        super().__init__(config)
        self._telephony = telephony
        self._from_number = from_number
        self._status_callback_url = status_callback_url
        self._prompt_builder = prompt_builder
        self._persona = persona
        self._notifications = notifications

    async def can_handle(self, message: AgentMessage) -> bool:
        # Never a delegation target (internal, no capabilities); satisfies BaseAgent only.
        return False

    async def execute(self, purpose: str, ticket: str, answer_url: str, to_number: str) -> AgentResponse:
        # Both callback URLs carry the ticket in their QUERY STRING — Twilio
        # POSTs only its own fields (CallSid, AnsweredBy, CallStatus, ...) in
        # the body and leaves the configured URL's query string untouched, so
        # this is the only channel that reaches the webhooks (which read it
        # back with `request.args`, not `await request.form`).
        #
        # The status callback needs it for the same reason `/voice/answer`
        # does: it is the only correlation path from a call-status event back
        # to the ticket/user whose one-call marker must be released when the
        # callback rings out, is busy, or fails at the carrier and
        # `/voice/answer` is therefore never reached at all. CallSid cannot
        # serve — it only exists once this very call returns.
        ticket_qs = urlencode({"ticket": ticket})
        call_sid = await self._telephony.originate_call(
            to=to_number,
            from_=self._from_number,
            answer_url=f"{answer_url}?{ticket_qs}",
            status_callback_url=f"{self._status_callback_url}?{ticket_qs}",
        )
        return AgentResponse.success(
            task_id=str(uuid4()), agent_id=self.agent_id, result={"call_sid": call_sid, "purpose": purpose},
        )

    async def session_config(self, user_id: str, account_id: str) -> Dict[str, Any]:
        """Instructions + tools for the realtime session. Raises on failure: no call
        opens on an empty context."""
        context = await self._persona.assemble(user_id, account_id)
        instructions = await self._prompt_builder.build_for_agent(
            agent_type="lelik",
            user_id=user_id,
            account_id=account_id,
            biographical_facts=context.biographical_facts,
            conversation_history=context.conversation_history,
            include_biographical=True,
            include_directives=True,
            include_datetime=True,
        )
        available = self.coordinator.get_available_intents_for(self._descriptor) if self.coordinator else []
        tools = [self._build_delegate_tool_declaration(available)] if available else []
        return {"instructions": instructions, "tools": tools}

    async def delegate(
        self, user_id: str, account_id: str, arguments: Dict[str, Any], call_context: List[Dict[str, str]],
    ) -> str:
        """One delegate_to_specialist call from the live session, run exactly as a text
        orchestrator's would be."""
        context: Dict[str, Any] = {
            "user_id": user_id,
            "account_id": account_id,
            self.coordinator.CALL_CHAIN_KEY: [self._descriptor.agent_id],
        }
        channel = await self._persona.primary_channel(user_id)
        if channel is not None:
            context.update(
                session_id=f"{user_id}:{channel.channel_id}",
                origin_channel_id=channel.channel_id,
                origin_platform=channel.platform,
            )
        # "later" has no delivery path for a SYNC-declared intent (AgentWorkerHandler
        # delivers only generator intents), so on the phone it would drop the answer.
        args = {k: v for k, v in arguments.items() if k != "mode"}
        args["context"] = {**normalize_delegate_context(arguments.get("context")), "call_context": call_context}
        result = await DelegationEngine(self.coordinator).dispatch(
            ToolCall(name=_DELEGATE_TOOL, args=args),
            context,
            dict(self._descriptor.intent_remap),
            dict(self._descriptor.intent_fanout),
            self.agent_id,
        )
        # ask_alek already copies Smart's own structured answer via notify_answer_copy
        # (AlekGatewayAgent) — posting again here would double it.
        if arguments.get("intent") != Intent.ASK_ALEK:
            await self._copy_links_to_chat(user_id, account_id, result.result_str)
        return result.result_str

    async def _copy_links_to_chat(self, user_id: str, account_id: str, result_str: str) -> None:
        try:
            links = extract_result_links(result_str)
            if not links:
                return
            text = "\n".join(f"[{link['anchor']}] {link['title']}" for link in links)
            await self._notifications.notify_answer_copy(
                user_id, account_id, SmartResponse(text=text, link_list=links),
            )
        except Exception as exc:
            # The spoken result_str is already decided and returned regardless of this failing.
            logger.error(f"[Lelik] link chat copy failed for {(user_id or '')[:8]}: {exc}", exc_info=True)
