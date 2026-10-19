"""DriveImageTriageAgent, triage v2 (USER_DRIVE_INDEXING_RFC §25.4.3, §25.4.4, OQ-V1): a batch of thumbnails, each
after its "Image N" label; a verdict per index and, for a photograph, a description, objects with their spellings, a
scene and the visible text, mapped back by index; byte-identical images in one batch must get one verdict (the
caption-shift check); one correction turn; the output-limit signal; billed per batch."""
import pytest

from src.agents.drive_image_triage_agent import AGENT_TYPE, DriveImageTriageAgent, TriageInput, _RESPONSE_SCHEMA
from src.domain.agent import AgentConfig
from src.domain.drive_enrichment import PhotoDescription, PhotoObject
from src.domain.drive_extraction import DriveModelUnavailable
from src.domain.drive_index import TriageVerdict
from src.domain.drive_index_params import (
    PHOTO_DESCRIPTION_CHARS, PHOTO_OBJECT_FORMS_MAX, PHOTO_OBJECTS_MAX, PHOTO_SCENE_WORDS, PHOTO_VISIBLE_TEXT_CHARS,
)
from src.domain.exceptions import LLMClientError, LLMServerError
from src.domain.llm import FinishReason
from src.infrastructure.agent_config import DRIVE_TRIAGE
from src.infrastructure.drive_model_call import DriveInputRefused, DriveOutputInvalid
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

TEXT, PHOTO = TriageVerdict.TEXT, TriageVerdict.PHOTO


def _images(n, same=()):
    """`same`: indexes whose bytes equal image 1's."""
    return [TriageInput(index=i, data=bytes([1 if i in same else i]) * 3, mime_type="image/jpeg",
                        camera_prior=i % 2 == 0) for i in range(1, n + 1)]


def _photo(i, description="a red bicycle by a wall", **kw):
    return {"index": i, "verdict": "photo", "description": description,
            "objects": kw.get("objects", [{"name": "bicycle", "forms": ["bicicleta", "велосипед"]}]),
            "scene": kw.get("scene", "street"), "visible_text": kw.get("visible_text", "")}


def _text(i):
    return {"index": i, "verdict": "text", "description": "", "objects": [], "scene": "", "visible_text": ""}


def _answer(*entries):
    return reply({"images": list(entries)})


def _agent(script):
    llm = ScriptedLLM(script)
    agent = DriveImageTriageAgent(AgentConfig(agent_id="drive_triage_u1", agent_type=AGENT_TYPE), context(llm),
                                  prompt_builder())
    return agent, llm


class TestRequest:
    async def test_each_thumbnail_follows_its_label(self):
        agent, llm = _agent([_answer(_photo(1), _text(2))])
        await agent.classify(_images(2), account_id="acc")
        parts = llm.requests[0].messages[0].parts
        assert parts[0].text == "Image 1: camera metadata absent" and parts[1].file_data["mime_type"] == "image/jpeg"
        assert parts[2].text == "Image 2: camera metadata present" and parts[3].file_data is not None
        assert "2 images" in parts[4].text
        req = llm.requests[0]
        assert req.response_schema is _RESPONSE_SCHEMA and req.response_mime_type == "application/json"
        assert (req.max_tokens, req.timeout, req.thinking) == (DRIVE_TRIAGE.max_tokens, DRIVE_TRIAGE.call_timeout_s,
                                                               "low")
        assert req.allow_provider_storage is False


class TestMapping:
    async def test_photo_fields_by_index_and_none_for_text(self):
        agent, _ = _agent([_answer(_text(2), _photo(1, visible_text="CAFÉ  ROMA"))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.results == {
            1: (PHOTO, PhotoDescription(description="a red bicycle by a wall",
                                        objects=(PhotoObject("bicycle", ("bicicleta", "велосипед")),),
                                        scene="street", visible_text="CAFÉ ROMA")),
            2: (TEXT, None)}
        assert out.missing == () and out.calls == 1

    async def test_fields_are_cut_to_their_caps(self):
        long = _photo(1, description="d" * 500, scene=" ".join(f"s{i}" for i in range(10)),
                      visible_text="v" * 400,
                      objects=[{"name": f"o{i}", "forms": [f"f{j}" for j in range(10)]} for i in range(12)])
        agent, _ = _agent([_answer(long)])
        photo = (await agent.classify(_images(1), account_id="acc")).results[1][1]
        assert len(photo.description) == PHOTO_DESCRIPTION_CHARS and len(photo.visible_text) == \
            PHOTO_VISIBLE_TEXT_CHARS
        assert len(photo.scene.split()) == PHOTO_SCENE_WORDS and len(photo.objects) == PHOTO_OBJECTS_MAX
        assert len(photo.objects[0].forms) == PHOTO_OBJECT_FORMS_MAX

    async def test_unknown_index_ignored_and_first_answer_wins(self):
        agent, llm = _agent([_answer(_photo(1, "first"), _text(1), _text(9))])
        out = await agent.classify(_images(1), account_id="acc")
        assert out.results[1][1].description == "first" and len(llm.requests) == 1

    async def test_missing_indexes_get_one_correction_turn(self):
        agent, llm = _agent([_answer(_photo(1)), _answer(_text(2))])
        out = await agent.classify(_images(2), account_id="acc")
        assert set(out.results) == {1, 2}
        assert "left out indexes [2]" in llm.requests[1].messages[-1].parts[0].text

    async def test_still_missing_is_reported(self):
        agent, _ = _agent([_answer(_photo(1)), _answer(_photo(1))])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.missing == (2,) and set(out.results) == {1}

    async def test_invalid_twice_is_invalid(self):
        agent, _ = _agent([reply(text="x"), reply(text="x")])
        with pytest.raises(DriveOutputInvalid):
            await agent.classify(_images(1), account_id="acc")

    async def test_the_output_limit_is_reported(self):
        agent, _ = _agent([reply(text='{"images": [', finish=FinishReason.MAX_TOKENS)])
        out = await agent.classify(_images(2), account_id="acc")
        assert out.output_limited and out.results == {}


class TestIdenticalImages:
    async def test_identical_images_with_one_verdict_pass(self):
        agent, llm = _agent([_answer(_photo(1), _text(2), _photo(3, "the same bicycle"))])
        out = await agent.classify(_images(3, same=(3,)), account_id="acc")
        assert set(out.results) == {1, 2, 3} and len(llm.requests) == 1

    async def test_different_verdicts_on_identical_images_are_asked_again(self):
        agent, llm = _agent([_answer(_photo(1), _text(2), _text(3)), _answer(_photo(1), _photo(3))])
        out = await agent.classify(_images(3, same=(3,)), account_id="acc")
        assert out.results[3][0] is PHOTO and out.results[2][0] is TEXT and out.missing == ()
        assert "identical images [1, 3]" in llm.requests[1].messages[-1].parts[0].text

    async def test_still_different_after_the_correction_settles_them_as_text(self):
        # Review m1 (arbiter, 2026-10-13): twins still disagreeing after the one correction turn are settled as
        # `text`, so the reader looks at them; they are no longer left unsettled to be retried every tick.
        agent, llm = _agent([_answer(_photo(1), _text(2), _text(3)), _answer(_photo(1), _text(3))])
        out = await agent.classify(_images(3, same=(3,)), account_id="acc")
        assert out.missing == () and set(out.results) == {1, 2, 3}
        assert out.results[1][0] is TEXT and out.results[3][0] is TEXT and out.results[2][0] is TEXT
        assert out.results[1][1] is None and out.results[3][1] is None
        assert len(llm.requests) == 2


class TestFailures:
    async def test_refused(self):
        agent, _ = _agent([LLMClientError("no", http_status=400)])
        with pytest.raises(DriveInputRefused):
            await agent.classify(_images(2), account_id="acc")

    async def test_outage(self):
        agent, _ = _agent([LLMServerError("500", http_status=500)])
        with pytest.raises(DriveModelUnavailable):
            await agent.classify(_images(2), account_id="acc")


def test_the_schema_requires_every_field_and_has_no_null():
    item = _RESPONSE_SCHEMA["properties"]["images"]["items"]
    # TRIAGE_VERSION 3 (USER_DRIVE_INDEXING_RFC §26.3): every image is asked for its capture; D40: its scene labels
    assert set(item["required"]) == {"index", "verdict", "capture", "description", "objects", "scene",
                                     "scene_labels", "visible_text"}
    assert "null" not in str(_RESPONSE_SCHEMA)
