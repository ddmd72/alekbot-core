"""EmailCardRunner (slice 3): the pinned-model knob, the lazily built agent with the pinned model and no prompt-content
store, and a build failure as an outage with the model named."""
import pytest

from src.composition.email_card_runner import EmailCardRunner, card_writer_model
from src.domain.email_card_reply import CardWriterStatus
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply
from tests.unit.domain.email_card_fixtures import answer

PROMPT = ("class EmailCardAgent extends Agent {\n    cognitive_process {\n    rules\n    }\n"
          "    output_format {\n    json\n    }\n}")


class _Builder:
    def __init__(self, llm, fail=False):
        self.llm, self.fail, self.calls = llm, fail, []

    def build(self, agent_type, config):
        self.calls.append((agent_type, config))
        if self.fail:
            raise RuntimeError("no provider")
        return context(self.llm, "gemini-3.5-flash")


@pytest.mark.parametrize("raw, expected", [(None, "gemini-3.5-flash-lite"), ("", "gemini-3.5-flash-lite"),
                                           ("  ", "gemini-3.5-flash-lite"), (" gemini-3.5-flash ", "gemini-3.5-flash")])
def test_the_pinned_model_is_the_stripped_knob_else_the_default(raw, expected):
    assert card_writer_model(raw) == expected


async def test_the_agent_is_built_once_with_the_pinned_model_a_default_user_and_no_content_store():
    llm = ScriptedLLM([reply(answer()), reply(answer())])
    builder = _Builder(llm)
    runner = EmailCardRunner(builder, prompt_builder(PROMPT), "gemini-3.5-flash-lite")

    first = await runner.write("acc1", "BLOCK", "<mail/>")
    await runner.write("acc1", "BLOCK", "<mail/>")

    assert first.status is CardWriterStatus.OK and first.answer is not None
    assert len(builder.calls) == 1 and builder.calls[0][0] == "email_card"
    assert builder.calls[0][1] == UserBotConfig()
    assert llm.requests[0].model_name == "gemini-3.5-flash-lite"
    agent = runner._agent
    assert agent is not None and agent._prompt_content_store is None


async def test_a_build_failure_is_an_outage_naming_the_model_and_is_retried_next_time():
    llm = ScriptedLLM([reply(answer())])
    builder = _Builder(llm, fail=True)
    runner = EmailCardRunner(builder, prompt_builder(PROMPT), "gemini-3.5-flash-lite")

    out = await runner.write("acc1", "BLOCK", "<mail/>")
    assert out.status is CardWriterStatus.UNAVAILABLE and out.model == "gemini-3.5-flash-lite" and out.calls == 0

    builder.fail = False
    assert (await runner.write("acc1", "BLOCK", "<mail/>")).status is CardWriterStatus.OK
