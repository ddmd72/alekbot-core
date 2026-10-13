"""DriveImageTriageAgent (USER_DRIVE_INDEXING_RFC §5.3, §24.4, §24.5; triage v2 §25.4.3, §25.4.4): a batch of
thumbnails with opaque indexes and the camera prior in, one verdict per index out, a description for photographs
(v2 replaced the ten-word caption); mapping back by index with unknown, repeated and missing indexes handled
deterministically; one correction turn; a stopped answer reported for the runner to halve; billed per batch. The v2
photo fields and the identical-image check: test_drive_image_triage_agent_v2.py."""
import json

import pytest

from src.agents.drive_image_triage_agent import AGENT_TYPE, DriveImageTriageAgent, TriageInput
from src.infrastructure.drive_model_call import DriveInputRefused, DriveModelCallFailed
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import PHOTO_DESCRIPTION_CHARS
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_TRIAGE
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

TEXT, PHOTO = TriageVerdict.TEXT, TriageVerdict.PHOTO


def _images(n, prior=True):
    return [TriageInput(index=i, data=bytes([i]) * 3, mime_type="image/jpeg", camera_prior=prior and i % 2 == 0)
            for i in range(1, n + 1)]


def _answer(*entries):
    """(index, verdict, description) entries in the v2 shape (§25.4.3), objects, scene and visible text empty."""
    return reply({"images": [{"index": i, "verdict": v, "description": c, "objects": [], "scene": "",
                              "visible_text": ""} for i, v, c in entries]})


def _photo(description):
    return PHOTO, PhotoDescription(description=description)


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
        out = await agent.classify([], account_id="acc")
        assert (out.results, out.missing, out.output_limited, out.calls) == ({}, (), False, 0)
        assert llm.requests == []


class TestMapping:
    async def test_verdicts_by_index_and_descriptions_for_photos_only(self):
        agent, _ = _agent([_answer((2, "text", "a receipt"), (1, "photo", "  Two  kids on a beach  "))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.results == {1: _photo("Two kids on a beach"), 2: (TEXT, None)}

    async def test_description_is_cut_to_its_character_cap(self):
        """v2 (§25.4.3): a description of at most PHOTO_DESCRIPTION_CHARS characters replaces the ten-word caption."""
        words = " ".join(f"w{i}" for i in range(200))
        agent, _ = _agent([_answer((1, "photo", words))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.results[1][1].description == words[:PHOTO_DESCRIPTION_CHARS]

    async def test_unknown_index_ignored_and_first_answer_wins(self):
        agent, llm = _agent([_answer((1, "photo", "first"), (1, "text", ""), (99, "text", ""))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.results == {1: _photo("first")} and len(llm.requests) == 1

    async def test_missing_indexes_get_one_correction_turn(self):
        agent, llm = _agent([_answer((1, "photo", "a cat")), _answer((2, "text", ""), (3, "photo", "a car"))])
        out = await agent.classify(_images(3), account_id="acc")
        assert out.results == {1: _photo("a cat"), 2: (TEXT, None), 3: _photo("a car")} and out.missing == ()
        correction = llm.requests[1].messages[-1].parts[0].text
        assert "left out indexes [2, 3]" in correction and "2, 3" in correction

    async def test_still_missing_after_the_correction_are_reported(self):
        """v2 (§25.13.1): an image still left out is reported missing — unsettled and retried, not a photograph."""
        agent, llm = _agent([_answer((1, "photo", "a cat")), _answer((1, "text", ""))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.results == {1: _photo("a cat")} and out.missing == (2,) and len(llm.requests) == 2

    async def test_invalid_json_then_valid(self):
        agent, llm = _agent([reply(text="not json"), _answer((1, "text", ""))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.results == {1: (TEXT, None)} and out.calls == 2
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

    async def test_a_safety_stop_is_a_refusal_of_the_batch(self):
        agent, _ = _agent([reply(text='{"images": [', finish=FinishReason.SAFETY)])
        with pytest.raises(DriveInputRefused):
            await agent.classify(_images(1), account_id="acc")

    async def test_an_answer_stopped_at_the_output_cap_is_reported_not_parsed(self):
        """v2 (§25.4.4): a reply stopped at the cap is reported for the runner to halve the batch, never parsed."""
        agent, llm = _agent([reply(text='{"images": [', finish=FinishReason.MAX_TOKENS)])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.output_limited and (out.results, out.missing, out.calls) == ({}, (), 1) and len(llm.requests) == 1


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
    assert out.usd == pytest.approx(1000 * 0.10e-6 + 200 * 0.50e-6) and out.fallback_usd == 0.0
    assert agent._quota_service.record_usage.await_args.kwargs["account_id"] == "acc-7"
