"""InteractiveChoiceService — what happens to an owner's pick on an interactive message, once the platform's route has
queued it (Facts S4 plan §3.20).

Three steps and no rule of its own: the platform user is resolved to a user of ours (`PlatformAuthPort`), the handler of the
binding's kind acts on the choice, and the message is re-rendered through `InteractiveMessagePort` as the handler's
outcome says. What a choice means is the handler's; how the message is redrawn is the port's."""
from typing import Dict, Mapping

from ..domain.interactive_choice import InteractiveChoice
from ..domain.prompt import ANONYMOUS_ACCOUNT_ID
from ..ports.choice_handler_port import ChoiceHandlerPort
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.platform_auth_port import PlatformAuthPort
from ..utils.logger import logger

UNKNOWN_USER = "unknown_user"
UNKNOWN_KIND = "unknown_kind"
IGNORED = "ignored"
SHOWN = "shown"


class InteractiveChoiceService:
    """`handle(choice)` -> `{"status": ...}`: `unknown_user` (the platform user is not one of ours: nothing is done),
    `unknown_kind` (no handler for the binding's kind: WARNING, nothing is done), `ignored` (the handler had nothing to
    show) or `shown`. An error of the handler or of the platform raises, so the task is retried; a handler is
    idempotent for the choice it was given."""

    def __init__(
        self, iam: PlatformAuthPort, handlers: Mapping[str, ChoiceHandlerPort], interactive: InteractiveMessagePort,
    ) -> None:
        self._iam = iam
        self._handlers = dict(handlers)
        self._interactive = interactive

    async def handle(self, choice: InteractiveChoice) -> Dict[str, str]:
        decision = await self._iam.authorize(choice.platform, platform_user_id=choice.platform_user_id)
        user = decision.user
        if decision.action != "allow" or user is None:
            logger.warning(f"[InteractiveChoice] a choice from an unknown {choice.platform} user; ignored")
            return {"status": UNKNOWN_USER}
        handler = self._handlers.get(choice.binding.kind)
        if handler is None:
            logger.warning(f"[InteractiveChoice] no handler for binding kind {choice.binding.kind!r}; ignored")
            return {"status": UNKNOWN_KIND}
        outcome = await handler.handle(choice, user.user_id, user.account_id or ANONYMOUS_ACCOUNT_ID)
        if outcome is None:
            return {"status": IGNORED}
        await self._interactive.show_choice(choice.message_ref, outcome)
        return {"status": SHOWN}
