"""InteractiveMessageRouter (Facts S4 · T5a): a lookup by platform and nothing else."""
import pytest

from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_binding import InteractiveBinding
from src.domain.interactive_message import InteractiveMessage
from src.domain.interactive_option import InteractiveOption
from src.domain.messaging import MessageRef
from src.infrastructure.interactive_message_router import InteractiveMessageRouter
from tests.support.fact_review_world import RecordingInteractiveMessagePort


def question():
    return InteractiveMessage(
        prompt="Is this still true?", quote="Lives in Lisbon",
        options=[InteractiveOption(id="a", label="yes"), InteractiveOption(id="b", label="no")],
        binding=InteractiveBinding(kind="fact_question", id="q1"),
    )


@pytest.fixture
def parts():
    slack, telegram = RecordingInteractiveMessagePort(), RecordingInteractiveMessagePort()
    return slack, telegram, InteractiveMessageRouter({"slack": slack, "telegram": telegram})


async def test_a_post_goes_to_the_adapter_of_its_platform_only(parts):
    slack, telegram, router = parts
    ref = await router.post("telegram", "555", question())
    assert ref == MessageRef("telegram", "555", "m1")
    assert [r for r, _ in telegram.posted] == [ref] and slack.posted == []


async def test_showing_a_choice_goes_to_the_adapter_of_the_refs_platform(parts):
    slack, telegram, router = parts
    ref = await router.post("slack", "D1", question())
    outcome = ChoiceOutcome(message=question(), chosen_option_id="a")
    await router.show_choice(ref, outcome)
    assert slack.shown == {ref: outcome} and telegram.shown == {}
    assert not slack.has_controls(ref)


async def test_an_unknown_platform_is_a_value_error_for_both_calls(parts):
    _, _, router = parts
    with pytest.raises(ValueError, match="whatsapp"):
        await router.post("whatsapp", "1", question())
    with pytest.raises(ValueError, match="whatsapp"):
        await router.show_choice(MessageRef("whatsapp", "1", "1"), ChoiceOutcome(message=question(), note="Closed."))


async def test_an_adapters_error_reaches_the_caller_unchanged(parts):
    slack, _, router = parts
    slack.fail = True
    with pytest.raises(RuntimeError, match="post failed"):
        await router.post("slack", "D1", question())


async def test_the_router_keeps_its_own_copy_of_the_mapping():
    adapters = {"slack": RecordingInteractiveMessagePort()}
    router = InteractiveMessageRouter(adapters)
    adapters.clear()
    assert (await router.post("slack", "D1", question())).platform == "slack"
