"""
AlekGatewayAgent — Alek as a specialist behind `ask_alek` (docs/10_rfcs/VOICE_COMPANION_RFC.md §4.7).

Zero-LLM. Routes the commission to router_agent_{user_id} exactly as ConversationHandler
does: the Router is where enrich_context (RRF memory search) runs, and Smart without it is
Alek without memory. The delegation context is forwarded whole, so `_call_chain` reaches
Smart and the coordinator's cycle guard covers this hop. The primary session is read, never
written: only ConversationHandler writes history.

ask_alek: reading-shaped answers (links, tables) are copied to chat here, the moment Alek
answers (§4.10 rule 2); the relay never posts to chat.

tell_alek (§4.15.2): an errand the caller is not waiting for. The commission says so, and
Alek's whole answer is posted to chat — it is the only place the caller will see the outcome.

Drive notes (DRIVE_FILE_NOTES_RFC §4.9, D18): on both paths, a note receipt or an erase request in Smart's answer is
posted to chat through UserNotificationService.notify_drive_note_receipt, independent of the answer copy. A failed
post is logged at ERROR and never fails the answer (the note is stored).
"""
import asyncio
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, AgentStatus, DeliveryItem
from ..domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY
from ..domain.delegation_timestamp import strip_delegation_timestamp
from ..domain.llm import MessagePart
from ..domain.messaging import SmartResponse
from ..domain.result_links import build_link_copy
from ..domain.retry_policy import NO_RETRY_POLICY
from ..domain.turn_identity import TURN_ID_CONTEXT_KEY
from ..infrastructure.agent_manifest import Intent
from ..utils.logger import logger
from .base_agent import BaseAgent

if TYPE_CHECKING:
    from ..services.user_notification_service import UserNotificationService

# The whole ask_alek path (gateway config + the message it routes to Router -> Smart; a message
# timeout wins over each agent's own config, BaseAgent._execute_with_timeout). It must outlast the
# relay's 300 s wait, since a later answer is posted to chat, stay under ~1200 s (the late-answer
# markers' TTL plus margin) and under Cloud Run's 1800 s request timeout. Owner, 2026-09-28.
ASK_ALEK_TIMEOUT_MS = 600_000

# The spoken answer must not wait longer than this for its chat copy to land.
_ANSWER_COPY_TIMEOUT_S = 5.0
# An errand's chat post IS its result and nobody is waiting on the line: a slow post must not drop it.
_ERRAND_POST_TIMEOUT_S = 30.0


_QUESTION_HEADER = "[Asked by Lelik during a phone call with the user."
_ERRAND_HEADER = ("[Errand from Lelik during a phone call with the user. They are not waiting for it "
                  "on the line: your answer goes to their chat.")


def _commission(query: str, reasoning: Optional[str], call_context: List[Dict[str, str]],
                errand: bool = False) -> str:
    # The coordinator stamped the query; Smart stamps the turn itself — one timestamp, not two.
    header = _ERRAND_HEADER if errand else _QUESTION_HEADER
    lines = [strip_delegation_timestamp(query), "", header]
    if call_context:
        # RFC §4.15.4: the full rule lives in Smart's PROTOCOL_VOICE_PARTNER (lelik_request_quality);
        # this line points at it, next to the evidence it asks Alek to weigh.
        lines.append("The request above was written by Lelik's faster, weaker model and may be imprecise. "
                     "Infer what the user wants from their own lines below and your chat history, and "
                     "serve that — their words win over Lelik's.")
        lines.append("The last exchanges on the call:")
        lines.extend(f"{e.get('role', 'user')}: {e.get('text', '')}" for e in call_context)
    if reasoning:
        lines.append(f"Lelik's note: {reasoning}")
    lines[-1] += "]"
    return "\n".join(lines)


class AlekGatewayAgent(BaseAgent):
    """ask_alek → Router → Smart, with the call's context attached."""

    # A retry re-runs Router + Smart + specialists: double spend, double chat copy.
    RETRY_POLICY = NO_RETRY_POLICY

    def __init__(self, config: AgentConfig, notification_service: "UserNotificationService") -> None:
        super().__init__(config)
        self._notifications = notification_service

    async def can_handle(self, message: AgentMessage) -> bool:
        # DELEGATE: a tell_alek errand arrives through the Cloud Task worker (AgentWorkerHandler).
        return message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE) and bool(message.payload.get("query"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        user_id = message.context.get("user_id")
        account_id = message.context.get("account_id")
        errand = message.payload.get("intent") == Intent.TELL_ALEK
        origin = "voice:tell_alek" if errand else "voice:ask_alek"
        text = _commission(
            message.payload.get("query", ""),
            message.payload.get("reasoning"),
            message.payload.get("call_context") or [],
            errand=errand,
        )
        routed = AgentMessage.create(
            sender=self.agent_id,
            recipient=f"router_agent_{user_id}",
            intent=AgentIntent.QUERY,
            payload={"text": text, "attachments": []},
            # Smart builds its user turn from current_message_parts only; run_origin labels the workspace writes
            # this run makes (AGENT_WORKSPACE_RFC §9).
            context={
                **message.context,
                "current_message_parts": [MessagePart(text=text)],
                "run_origin": origin,
                # The call's turn (LelikAgent fixed it per delegation); this message's own id when it came without one.
                TURN_ID_CONTEXT_KEY: message.context.get(TURN_ID_CONTEXT_KEY) or f"{origin}:{message.task_id}",
            },
            # Explicit, so Smart's own 300 s config cap does not cut an answer the relay abandoned.
            timeout_ms=ASK_ALEK_TIMEOUT_MS,
        )
        response = await self.coordinator.route_message(routed)
        if response.status != AgentStatus.SUCCESS:
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id,
                error=f"Alek did not answer: {response.error}",
            )
        summary_task = (response.metadata or {}).get("response_summary_task")
        if summary_task:
            # Nothing writes history on this path; the summary would be paid for and dropped.
            summary_task.cancel()
        answer = response.result
        if isinstance(answer, SmartResponse):
            if errand:
                # The chat is where the caller reads an errand's outcome: all of it, always.
                copy = answer
            else:
                # link_list/structured_data are Smart's own reading-shaped answer; otherwise fall
                # back to scanning the plain text for links.
                copy = answer if (answer.link_list or answer.structured_data) else build_link_copy(answer.text)
            if copy is not None:
                try:
                    await asyncio.wait_for(
                        self._notifications.notify_answer_copy(user_id, account_id, copy),
                        timeout=_ERRAND_POST_TIMEOUT_S if errand else _ANSWER_COPY_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[AlekGateway] chat copy timed out for {(user_id or '')[:8]}")
                except Exception as exc:
                    # The spoken answer matters more than its chat copy.
                    logger.error(f"[AlekGateway] chat copy failed for {(user_id or '')[:8]}: {exc}", exc_info=True)
        await self._post_drive_note_items(user_id, account_id, response.delivery_items,
                                          _ERRAND_POST_TIMEOUT_S if errand else _ANSWER_COPY_TIMEOUT_S)
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result=answer)

    async def _post_drive_note_items(self, user_id: Optional[str], account_id: Optional[str],
                                     items: Sequence[DeliveryItem], timeout_s: float) -> None:
        """Each drive-note receipt and erase request goes to chat (§4.9): the owner sees what was stored at once. The
        posts run in order under ONE `timeout_s` for all of them (final review C-M10): on `ask_alek` they hold the
        caller's answer, so several notes never add one wait each. An item not posted in time is logged at ERROR."""
        notes = [item for item in items or () if item.type in (DRIVE_NOTE_RECEIPT_DELIVERY, DRIVE_NOTE_ERASE_DELIVERY)]
        if not notes:
            return
        posted = 0

        async def post_all() -> None:
            nonlocal posted
            for item in notes:
                try:
                    await self._notifications.notify_drive_note_receipt(user_id or "", account_id or "", item)
                except Exception as exc:
                    logger.error(f"[AlekGateway] drive note {item.type} post failed for {(user_id or '')[:8]}: "
                                 f"{exc}", exc_info=True)
                posted += 1

        try:
            await asyncio.wait_for(post_all(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.error(f"[AlekGateway] drive note posts timed out for {(user_id or '')[:8]}: "
                         f"{len(notes) - posted} of {len(notes)} not posted")
