"""DriveImageTriageAgent (USER_DRIVE_INDEXING_RFC §5.3, §24.4, §24.5): a batch of thumbnails with opaque indexes and
the camera prior in, one verdict per index out, a caption of at most ten words for photographs; mapping back by index
with unknown, repeated and missing indexes handled deterministically; one correction turn; billed per batch."""
import json

import pytest

from src.agents.drive_image_triage_agent import AGENT_TYPE, DriveImageTriageAgent, TriageInput
from src.infrastructure.drive_model_call import DriveModelCallFailed
from src.domain.agent import AgentConfig
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_index import TriageVerdict
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_TRIAGE
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

TEXT, PHOTO = TriageVerdict.TEXT, TriageVerdict.PHOTO


def _images(n, prior=True):
    return [TriageInput(index=i, data=bytes([i]) * 3, mime_type="image/jpeg", camera_prior=prior and i % 2 == 0)
            for i in range(1, n + 1)]


def _answer(*entries):
    return reply({"images": [{"index": i, "verdict": v, "caption": c} for i, v, c in entries]})


def _agent(script, builder=None):
    llm = ScriptedLLM(script)
    agent = DriveImageTriageAgent(AgentConfig(agent_id="drive_triage_u1", agent_type=AGENT_TYPE), context(llm),
                                  builder or prompt_builder())
    return agent, llm


class TestRequest:
    async def test_each_thumbnail_follows_its_index_and_prior_and_no_item_id_is_sent(self):
        agent, llm = _agent([_answer((1, "photo", "a dog"), (2, "text", ""))])
        await agent.classify(_images(2), account_id="acc")
        req = llm.requests[0]
        parts = req.messages[0].parts
        assert parts[0].text == "Image 1: camera metadata absent"
        assert parts[1].file_data == {"base64": "AQEB", "mime_type": "image/jpeg"}
        assert parts[2].text == "Image 2: camera metadata present"
        assert "2 images" in parts[4].text
        assert req.response_mime_type == "application/json" and req.response_schema["required"] == ["images"]
        assert req.thinking == "low" and req.max_tokens == DRIVE_TRIAGE.max_tokens
        assert req.allow_provider_storage is False

    async def test_empty_batch_makes_no_call(self):
        agent, llm = _agent([])
        assert (await agent.classify([], account_id="acc")).verdicts == {}
        assert llm.requests == []


class TestMapping:
    async def test_verdicts_by_index_and_captions_for_photos_only(self):
        agent, _ = _agent([_answer((2, "text", "a receipt"), (1, "photo", "  Two  kids on a beach  "))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.verdicts == {1: (PHOTO, "Two kids on a beach"), 2: (TEXT, "")}

    async def test_caption_is_cut_to_ten_words(self):
        agent, _ = _agent([_answer((1, "photo", " ".join(f"w{i}" for i in range(15))))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.verdicts[1][1] == " ".join(f"w{i}" for i in range(10))

    async def test_unknown_index_ignored_and_first_answer_wins(self):
        agent, llm = _agent([_answer((1, "photo", "first"), (1, "text", ""), (99, "text", ""))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.verdicts == {1: (PHOTO, "first")} and len(llm.requests) == 1

    async def test_missing_indexes_get_one_correction_turn(self):
        agent, llm = _agent([_answer((1, "photo", "a cat")), _answer((2, "text", ""), (3, "photo", "a car"))])
        out = await agent.classify(_images(3), account_id="acc")
        assert out.verdicts == {1: (PHOTO, "a cat"), 2: (TEXT, ""), 3: (PHOTO, "a car")}
        correction = llm.requests[1].messages[-1].parts[0].text
        assert "left out indexes [2, 3]" in correction and "2, 3" in correction

    async def test_still_missing_after_the_correction_are_left_out(self):
        agent, llm = _agent([_answer((1, "photo", "a cat")), _answer((1, "text", ""))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.verdicts == {1: (PHOTO, "a cat")} and len(llm.requests) == 2

    async def test_invalid_json_then_valid(self):
        agent, llm = _agent([reply(text="not json"), _answer((1, "text", ""))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.verdicts == {1: (TEXT, "")}
        assert "was not valid" in llm.requests[1].messages[-1].parts[0].text

    @pytest.mark.parametrize("bad", [
        '{"images": "x"}', '[]', json.dumps({"images": [{"index": "1", "verdict": "text"}]}),
        json.dumps({"images": [{"index": True, "verdict": "text"}]}),
        json.dumps({"images": [{"index": 1, "verdict": "screenshot"}]}),
    ])
    async def test_nothing_usable_after_the_correction_fails_the_batch(self, bad):
        agent, _ = _agent([reply(text=bad), reply(text=bad)])
        with pytest.raises(DriveModelCallFailed):
            await agent.classify(_images(1), account_id="acc")

    @pytest.mark.parametrize("finish", [FinishReason.MAX_TOKENS, FinishReason.SAFETY])
    async def test_a_stopped_answer_fails_the_batch(self, finish):
        agent, _ = _agent([reply(text='{"images": [', finish=finish)])
        with pytest.raises(DriveModelCallFailed):
            await agent.classify(_images(1), account_id="acc")


class TestFailures:
    async def test_refused_batch_is_per_input(self):
        agent, _ = _agent([LLMClientError("image too large", http_status=400)])
        with pytest.raises(DriveModelCallFailed):
            await agent.classify(_images(2), account_id="acc")

    async def test_server_error_is_an_outage(self):
        agent, _ = _agent([LLMServerError("529", http_status=529)])
        with pytest.raises(DriveModelUnavailable):
            await agent.classify(_images(2), account_id="acc")

    async def test_missing_prompt_is_an_outage(self):
        agent, llm = _agent([], prompt_builder(prompt="class X {\n\n}"))
        with pytest.raises(DriveModelUnavailable):
            await agent.classify(_images(1), account_id="acc")
        assert llm.requests == []


async def test_batch_is_billed_once_with_its_cost():
    from unittest.mock import AsyncMock
    agent, _ = _agent([_answer((1, "photo", "x")), ])
    agent._quota_service = AsyncMock()
    out = await agent.classify(_images(1), account_id="acc-7")
    assert out.cost == pytest.approx(1000 * 0.10e-6 + 200 * 0.50e-6)
    assert agent._quota_service.record_usage.await_args.kwargs["account_id"] == "acc-7"
