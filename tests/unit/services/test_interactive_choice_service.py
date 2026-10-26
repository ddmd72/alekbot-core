"""`InteractiveChoiceService` (Facts S4 · T8g, plan §3.20 "The worker"): a queued pick is resolved to a user, handed to the
handler of its binding kind, and the message is re-rendered as the handler's outcome says.

The identity check is a fake with the real service's decision shape (an allowed user carries a profile, anything else
none); the handler records what it was given and answers a scripted outcome; the interactive port is the review world's
recording fake, which refuses to update a message that was never posted, as a platform does."""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pytest

from src.domain.auth import IAMDecision
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_choice import InteractiveChoice
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef
from src.domain.user import UserProfile
from src.ports.choice_handler_port import ChoiceHandlerPort
from src.ports.platform_auth_port import PlatformAuthPort
from src.services.interactive_choice_service import InteractiveChoiceService
from tests.support.fact_review_world import RecordingInteractiveMessagePort

AT = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
REF = MessageRef(platform="slack", channel_id="D1", message_id="m1")
MESSAGE = InteractiveMessage(
    prompt="Is this still true?", quote="User trains at the Iron Temple gym.",
    options=(InteractiveOption(id="yes", label="Yes"), InteractiveOption(id="no", label="No")),
    binding=InteractiveBinding(kind="demo_kind", id="d1"), reply_hint="Reply with a digit.",
)


class FakeIam(PlatformAuthPort):
    """Registered platform users by (platform, id); everything else is rejected, as the real service rejects it."""

    def __init__(self, users: Dict[Tuple[str, str], UserProfile]) -> None:
        self.users = users
        self.asked: List[Tuple[str, Optional[str]]] = []

    async def authorize(self, platform: str, platform_user_id: Optional[str] = None) -> IAMDecision:
        self.asked.append((platform, platform_user_id))
        user = self.users.get((platform, platform_user_id or ""))
        if user is None:
            return IAMDecision(action="reject", message="Register first")
        return IAMDecision(action="allow", user=user)


class DemoHandler(ChoiceHandlerPort):
    def __init__(self, outcome: Optional[ChoiceOutcome]) -> None:
        self.outcome = outcome
        self.calls: List[Tuple[InteractiveChoice, str, str]] = []

    @property
    def kind(self) -> str:
        return "demo_kind"

    async def handle(self, choice, user_id, account_id):
        self.calls.append((choice, user_id, account_id))
        return self.outcome


def choice(kind: str = "demo_kind", platform_user_id: str = "U1", platform: str = "slack") -> InteractiveChoice:
    return InteractiveChoice(binding=InteractiveBinding(kind=kind, id="d1"), option_id="yes", platform=platform,
                             platform_user_id=platform_user_id, message_ref=MessageRef(platform, "D1", "m1"), at=AT)


class World:
    def __init__(self, outcome: Optional[ChoiceOutcome], account_id: Optional[str] = "acc-1") -> None:
        self.iam = FakeIam({("slack", "U1"): UserProfile(user_id="usr-1", email="a@example.com", account_id=account_id)})
        self.handler = DemoHandler(outcome)
        self.port = RecordingInteractiveMessagePort()
        self.service = InteractiveChoiceService(self.iam, {self.handler.kind: self.handler}, self.port)

    async def posted(self) -> MessageRef:
        return await self.port.post("slack", "D1", MESSAGE)


async def test_a_known_user_choice_is_handled_and_the_message_shows_the_outcome():
    outcome = ChoiceOutcome(message=MESSAGE, chosen_option_id="yes")
    world = World(outcome)
    ref = await world.posted()

    assert await world.service.handle(choice()) == {"status": "shown"}

    assert world.handler.calls == [(choice(), "usr-1", "acc-1")]
    assert world.port.shown == {ref: outcome}
    assert world.iam.asked == [("slack", "U1")]


async def test_an_unknown_slack_user_gets_nothing_and_no_handler_runs(caplog):
    world = World(ChoiceOutcome(message=MESSAGE, chosen_option_id="yes"))
    await world.posted()

    with caplog.at_level(logging.WARNING):
        assert await world.service.handle(choice(platform_user_id="U-stranger")) == {"status": "unknown_user"}

    assert world.handler.calls == [] and world.port.shown == {}
    assert any("unknown slack user" in r.getMessage() for r in caplog.records)
    assert "U-stranger" not in caplog.text


async def test_an_unknown_binding_kind_is_a_warning_and_nothing_is_done(caplog):
    world = World(ChoiceOutcome(message=MESSAGE, chosen_option_id="yes"))
    await world.posted()

    with caplog.at_level(logging.WARNING):
        assert await world.service.handle(choice(kind="other_kind")) == {"status": "unknown_kind"}

    assert world.handler.calls == [] and world.port.shown == {}
    assert any("'other_kind'" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


async def test_a_handler_with_nothing_to_show_leaves_the_message_alone():
    world = World(None)
    await world.posted()

    assert await world.service.handle(choice()) == {"status": "ignored"}

    assert len(world.handler.calls) == 1 and world.port.shown == {}


async def test_a_user_without_an_account_is_handled_in_the_anonymous_account():
    from src.domain.prompt import ANONYMOUS_ACCOUNT_ID

    world = World(None, account_id=None)

    await world.service.handle(choice())

    assert world.handler.calls[0][2] == ANONYMOUS_ACCOUNT_ID


async def test_a_platform_that_cannot_update_the_message_raises_so_the_task_is_retried():
    world = World(ChoiceOutcome(message=MESSAGE, chosen_option_id="yes"))     # the message was never posted

    with pytest.raises(RuntimeError, match="message_not_found"):
        await world.service.handle(choice())


async def test_a_handler_error_is_raised_and_nothing_is_shown():
    world = World(None)
    await world.posted()

    async def broken(choice, user_id, account_id):
        raise RuntimeError("store unavailable")

    world.handler.handle = broken                                   # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="store unavailable"):
        await world.service.handle(choice())
    assert world.port.shown == {}
