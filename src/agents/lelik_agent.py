"""
LelikAgent — the voice companion (docs/10_rfcs/VOICE_COMPANION_RFC.md §4.4, §4.7).

Built the way TutorAgent is: its own prompt over context LelikPersonaService assembles,
and the shared delegate_to_specialist tool over LELIK.allowed_intents. Its LLM call is
a realtime session in the relay process, so there are three entry points, not one:
execute() places the callback (§4.6), session_config() feeds the session at pickup
(§4.8), delegate() runs one tool call from the live session through DelegationEngine.

The session has two tools (§4.15.2, revised 2026-10-29): delegate_to_specialist over
LELIK.allowed_intents, and send_to_chat, a channel rather than a worker — the caller's chat. With the
LELIK_XAI_NATIVE_SEARCH switch on, a provider with a web search of its own (VoiceProviderProfile.builtin_web_search:
xAI) gets that search in the session, and search_web_light leaves delegate_to_specialist. The relay turns a
send_to_chat call into the errand intent of the same name (domain/voice_errand.py), so delegate() runs both the
same way.

With the VOICE_MCP_TOOLS knob on, the plain companion call on xAI also carries the drive MCP server
(LELIK_VOICE_MCP_RFC §4): session_config puts its descriptor (no token) under `mcp_servers`; the relay pairs it with
the token /voice/session-config mints at redemption. Memory is not an MCP server of any call: it goes by the
search_memory delegation, declared and permitted in every mode but the interpreter, which has no memory
(§2, amendment 2026-11-10).

execute() does not catch originate_call failures: the webhook's own try/except releases
the ticket and one-call marker.

delegate() posts nothing to chat (VOICE_COMPANION_RFC §4.15.3, owner decision 2026-09-29): the
automatic link copy of §4.10 rule 2 filled the chat with every search's links. When the caller
wants something in chat, Lelik calls send_to_chat, and Alek decides what is worth posting.
"""
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Dict, List, Sequence, Tuple
from urllib.parse import urlencode
from uuid import uuid4

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.llm import ToolCall
from ..domain.messaging import session_id_for
from ..domain.remote_tool_server import RemoteToolServer
from ..domain.turn_identity import TURN_ID_CONTEXT_KEY
from ..domain.voice_errand import SEND_TO_CHAT, SEND_TO_CHAT_REQUEST
from ..domain.voice_delegation_outcome import VoiceDelegationOutcome
from ..domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD
from ..domain.voice_mcp_token import voice_mcp_eligible
from ..domain.voice_mode import VoiceMode
from ..domain.voice_provider_profile import VoiceProviderProfile
from ..infrastructure.agent_manifest import ALEK, LELIK, Intent
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
        voice_profile: VoiceProviderProfile,
        native_web_search: bool = False,
        mcp_servers: Sequence[RemoteToolServer] = (),
    ) -> None:
        """voice_profile: the user's realtime provider — which prompt profile the session
        instructions are built from, and the spec the relay runs the call with
        (VOICE_MULTI_PROVIDER_RFC §4.2).
        native_web_search: the LELIK_XAI_NATIVE_SEARCH switch. On a provider whose profile has
        builtin_web_search, the session offers the provider's own search instead of search_web_light.
        mcp_servers: the drive MCP server descriptor (no token), given when the VOICE_MCP_TOOLS knob is on
        (LELIK_VOICE_MCP_RFC §4). Only an eligible call carries it (the plain companion call on xAI, O6/O7); it keeps
        its search_memory delegation."""
        super().__init__(config)
        self._native_web_search = native_web_search
        self._mcp_servers = tuple(mcp_servers)
        self._telephony = telephony
        self._from_number = from_number
        self._status_callback_url = status_callback_url
        self._prompt_builder = prompt_builder
        self._persona = persona
        self._notifications = notifications
        self._voice_profile = voice_profile

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

    async def session_config(self, user_id: str, account_id: str, mode: VoiceMode = VoiceMode.LELIK) -> Dict[str, Any]:
        """Instructions + tools for the realtime session. Raises on failure: no call
        opens on an empty context."""
        context = await self._persona.assemble(user_id, account_id)
        instructions = await self._prompt_builder.build_for_agent(
            agent_type=self._voice_profile.prompt_profile_for(mode),
            user_id=user_id,
            account_id=account_id,
            biographical_facts=context.biographical_facts,
            conversation_history=context.conversation_history,
            include_biographical=True,
            include_directives=False,
            include_datetime=True,
        )
        available = self.coordinator.get_available_intents_for(self._descriptor) if self.coordinator else []
        native_search = self._native_search_active()
        if native_search:
            # The provider searches the web itself inside the session; the delegated light search would be a second path.
            available = [intent for intent in available if intent["name"] != Intent.SEARCH_WEB_LIGHT]
        mcp_servers = self._mcp_servers_for(mode)
        if mode is VoiceMode.INTERPRETER:
            # An interpreter call has no memory at all (owner rule). Every other mode reaches memory by the
            # search_memory delegation only (LELIK_VOICE_MCP_RFC §2, amendment 2026-11-10); the drive is the one MCP server.
            available = [intent for intent in available if intent["name"] != Intent.SEARCH_MEMORY]
        tools = [self._build_delegate_tool_declaration(available)] if available else []
        if self.coordinator and self.coordinator.get_execution_mode(Intent.SEND_TO_CHAT) is not None:
            tools.append(_send_to_chat_tool_declaration())
        session = replace(self._voice_profile.session, builtin_search=native_search)
        config: Dict[str, Any] = {"instructions": instructions, "tools": tools, "voice": session.to_dict()}
        if mcp_servers:
            # Descriptors only: the tokens are minted at ticket redemption and never stored (O1).
            config[VOICE_MCP_SERVERS_FIELD] = [server.to_dict() for server in mcp_servers]
        return config

    def _native_search_active(self) -> bool:
        return bool(self._native_web_search and self._voice_profile.builtin_web_search)

    def _mcp_servers_for(self, mode: VoiceMode) -> Tuple[RemoteToolServer, ...]:
        """The MCP servers a call in `mode` carries: all of them for an eligible call, none otherwise."""
        if voice_mcp_eligible(mode.value, self._voice_profile.session.provider):
            return self._mcp_servers
        return ()

    def permitted_intents(self, mode: VoiceMode = VoiceMode.LELIK) -> frozenset:
        """The intents a live call in `mode` may delegate: what session_config declares (LELIK.allowed_intents, minus
        search_web_light when the provider searches natively, minus search_memory in an interpreter call, which has
        no memory) plus the errand intent of the send_to_chat tool. The declaration only shapes what the model is told;
        this is what /voice/delegate enforces."""
        allowed = set(LELIK.allowed_intents or ()) | {Intent.SEND_TO_CHAT}
        if self._native_search_active():
            allowed.discard(Intent.SEARCH_WEB_LIGHT)
        if mode is VoiceMode.INTERPRETER:
            allowed.discard(Intent.SEARCH_MEMORY)
        return frozenset(allowed)

    async def delegate(
        self, user_id: str, account_id: str, arguments: Dict[str, Any], call_context: List[Dict[str, str]],
        mode: VoiceMode = VoiceMode.LELIK,
    ) -> str:
        """One delegate_to_specialist call from the live session, run exactly as a text
        orchestrator's would be."""
        return (await self.delegate_outcome(user_id, account_id, arguments, call_context, mode=mode)).text

    async def delegate_outcome(
        self, user_id: str, account_id: str, arguments: Dict[str, Any], call_context: List[Dict[str, str]],
        mode: VoiceMode = VoiceMode.LELIK,
    ) -> VoiceDelegationOutcome:
        """`delegate`, plus whether the result is a failure: /voice/delegate keeps an answer
        for chat only when it is a real one (a failure's text is an error string). `mode`: the call's mode, which
        decides the allowlist (`permitted_intents`)."""
        intent = arguments.get("intent")
        if not isinstance(intent, str) or intent not in self.permitted_intents(mode):
            # Enum-like names only in the log: the argument is model output, never echoed as content.
            shown = intent[:40] if isinstance(intent, str) and intent.isidentifier() else "<invalid>"
            logger.warning(f"voice delegate refused: intent {shown!r} is outside the voice allowlist")
            return VoiceDelegationOutcome(text="Tool error: this request is not available in a voice call.", failed=True)
        context: Dict[str, Any] = {
            "user_id": user_id,
            "account_id": account_id,
            self.coordinator.CALL_CHAIN_KEY: [self._descriptor.agent_id],
            # One delegation of the call is one owner turn: fixed here, so a send_to_chat Cloud Task carries the same id
            # on a redelivery (the drive-note undo guard, DRIVE_FILE_NOTES_RFC §4.2).
            TURN_ID_CONTEXT_KEY: f"voice:{uuid4().hex}",
        }
        channel = await self._persona.primary_channel(user_id)
        if channel is not None:
            context.update(
                session_id=session_id_for(user_id, channel.channel_id),
                origin_channel_id=channel.channel_id,
                origin_platform=channel.platform,
            )
        args = dict(arguments)
        args["context"] = {**normalize_delegate_context(arguments.get("context")), "call_context": call_context}
        result = await DelegationEngine(self.coordinator).dispatch(
            ToolCall(name=_DELEGATE_TOOL, args=args),
            context,
            dict(self._descriptor.intent_remap),
            dict(self._descriptor.intent_fanout),
            self.agent_id,
        )
        return VoiceDelegationOutcome(text=result.result_str, failed=bool(result.failed))


def _send_to_chat_tool_declaration() -> Dict[str, Any]:
    """Lelik's own channel tool, declared beside delegate_to_specialist in the neutral shape the realtime
    adapters wrap. Its description is the errand intent's, from the manifest (one source)."""
    return {
        "name": SEND_TO_CHAT,
        "description": ALEK.capability_descriptions[Intent.SEND_TO_CHAT],
        "parameters": {
            "type": "object",
            "properties": {
                SEND_TO_CHAT_REQUEST: {
                    "type": "string",
                    "description": "What to put into the chat, in full: Alek does not hear the call.",
                },
            },
            "required": [SEND_TO_CHAT_REQUEST],
        },
    }
