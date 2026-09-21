"""
LelikAgent — places the callback that starts every voice-companion call
(RFC docs/10_rfcs/VOICE_COMPANION_RFC.md §4.6, §4.13).

Not a delegation-tool specialist: `internal=True` (see
`infrastructure/agent_manifest.py::LELIK`) with no `Intent` registered, so it
is never reachable via `delegate_to_specialist` or `AgentCoordinator`. The
only caller in Slice 1 is `src/web/voice_webhook_app.py::voice_auth`, which
constructs it via `lelik_agent_factory(user_id, account_id)` and awaits
`execute(purpose, ticket, answer_url)` directly. Registering an intent for
this agent is exactly what the deferred outbound RFC does, not Slice 1.

`execute()` does not catch `TelephonyPort.originate_call` failures — the
auth webhook's own try/except is what releases the ticket and one-call
marker on origination failure (commit 67beca7, mint-side counterpart to
`voice_control_plane_app.submit_transcript`'s release-on-failure guarantee).
Swallowing the exception here into an `AgentResponse.failure()` would leave
that ticket/marker stranded for up to `one_call_ttl_s` with no alert.

No LLM call happens here — Lelik's persona/instructions are assembled by
the answer webhook via `PromptBuilderPort.build_for_agent` (Task 9) once the
callback is actually answered, not by this agent. `execution_context` is
accepted for constructor parity with other `BaseAgent` subclasses but is not
used by `execute()`.
"""

from typing import Optional
from urllib.parse import urlencode
from uuid import uuid4

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..ports.llm_port import AgentExecutionContext
from ..ports.telephony_port import TelephonyPort
from .base_agent import BaseAgent


class LelikAgent(BaseAgent):
    """Originates the identity-confirming callback for a voice-companion session."""

    def __init__(
        self,
        config: AgentConfig,
        execution_context: Optional[AgentExecutionContext],
        telephony: TelephonyPort,
        from_number: str,
        status_callback_url: str,
        to_number: str,
    ) -> None:
        super().__init__(config)
        self._execution_context = execution_context
        self._telephony = telephony
        self._from_number = from_number
        self._status_callback_url = status_callback_url
        self._to_number = to_number

    async def can_handle(self, message: AgentMessage) -> bool:
        # Never routed via AgentCoordinator (internal=True, no Intent
        # registered) — this only satisfies BaseAgent's abstract contract.
        return False

    async def execute(self, purpose: str, ticket: str, answer_url: str) -> AgentResponse:
        answer_url_with_ticket = f"{answer_url}?{urlencode({'ticket': ticket})}"
        call_sid = await self._telephony.originate_call(
            to=self._to_number,
            from_=self._from_number,
            answer_url=answer_url_with_ticket,
            status_callback_url=self._status_callback_url,
        )
        return AgentResponse.success(
            task_id=str(uuid4()),
            agent_id=self.agent_id,
            result={"call_sid": call_sid, "purpose": purpose},
        )
