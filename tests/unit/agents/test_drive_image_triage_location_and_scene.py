"""Triage, S6: the photo's location as a prior (D39) and the fixed scene labels (D40). The label before each thumbnail
carries the place and the coordinates rounded to 3 decimals only when the record has them, never a camera model or a
date; `scene_labels` is an array of the fixed enum in the schema, and its parse drops unknown values, dedupes and caps
at six without ever being an error; a text image has none."""
import json

import pytest

from src.agents.drive_image_triage_agent import (
    AGENT_TYPE, DriveImageTriageAgent, TriageInput, _RESPONSE_SCHEMA, image_label,
)
from src.domain.agent import AgentConfig
from src.domain.drive_labels import SCENE_LABELS
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply


def _input(**kw):
    base = dict(index=3, data=b"abc", mime_type="image/jpeg", camera_prior=True)
    base.update(kw)
    return TriageInput(**base)


def _agent(script):
    llm = ScriptedLLM(script)
    return DriveImageTriageAgent(AgentConfig(agent_id="drive_triage_u1", agent_type=AGENT_TYPE), context(llm),
                                 prompt_builder()), llm


def _entry(i, verdict="photo", labels=None, **kw):
    entry = {"index": i, "verdict": verdict, "capture": "none", "description": "a beach" if verdict == "photo" else "",
             "objects": [], "scene": "beach" if verdict == "photo" else "", "visible_text": ""}
    if labels is not None:
        entry["scene_labels"] = labels
    entry.update(kw)
    return entry


class TestLocationPrior:
    def test_without_a_location_the_label_is_the_old_one(self):
        assert image_label(_input()) == "Image 3: camera metadata present"
        assert image_label(_input(camera_prior=False)) == "Image 3: camera metadata absent"

    def test_with_a_place_and_coordinates(self):
        label = image_label(_input(place_name="Porto, Portugal", location=(41.141, -8.611)))
        assert label == "Image 3: camera metadata present; taken near Porto, Portugal (41.141, -8.611)"

    def test_coordinates_without_a_place_name(self):
        assert image_label(_input(location=(41.403, -2.174))) == (
            "Image 3: camera metadata present; taken at (41.403, -2.174)")

    @pytest.mark.parametrize("lat,lon", [(41.40338291, 2.17402516), (41.4034999, 2.1745001), (-33.8688197, 151.2092955)])
    def test_the_coordinates_are_never_more_precise_than_three_decimals(self, lat, lon):
        label = image_label(_input(place_name="X", location=(lat, lon)))
        numbers = label.split("(")[-1].rstrip(")").split(", ")
        assert all(len(n.split(".")[1]) <= 3 for n in numbers), label
        assert numbers == [str(round(lat, 3)), str(round(lon, 3))]

    def test_no_camera_model_and_no_date_in_the_label(self):
        label = image_label(_input(place_name="Rome (Roma), Italy", location=(41.9, 12.5)))
        assert "Apple" not in label and "20" not in label.replace("41.9", "").replace("12.5", "")

    async def test_the_label_reaches_the_request_before_its_thumbnail(self):
        agent, llm = _agent([reply({"images": [_entry(3)]})])
        await agent.classify([_input(place_name="Porto, Portugal", location=(41.141, -8.611))], account_id="a")
        parts = llm.requests[0].messages[0].parts
        assert parts[0].text == "Image 3: camera metadata present; taken near Porto, Portugal (41.141, -8.611)"
        assert parts[1].file_data is not None


class TestSceneLabelsSchema:
    def test_the_array_is_required_and_its_items_are_the_fixed_enum(self):
        item = _RESPONSE_SCHEMA["properties"]["images"]["items"]
        assert "scene_labels" in item["required"]
        spec = item["properties"]["scene_labels"]
        assert spec["type"] == "array" and spec["items"]["enum"] == list(SCENE_LABELS)
        assert "null" not in json.dumps(spec)


class TestSceneLabelsParse:
    async def _labels(self, raw, verdict="photo"):
        agent, _ = _agent([reply({"images": [_entry(1, verdict, raw)]})])
        out = await agent.classify([_input(index=1)], account_id="a")
        return out.results[1][1]

    async def test_known_labels_are_kept_in_order(self):
        assert (await self._labels(["sunset", "beach"])).scene_labels == ("sunset", "beach")

    async def test_unknown_values_are_dropped_with_a_warning(self, caplog):
        photo = await self._labels(["sunset", "volcano", 7, "beach"])
        assert photo.scene_labels == ("sunset", "beach")

    async def test_duplicates_are_dropped(self):
        assert (await self._labels(["sea", "sea", "beach", "sea"])).scene_labels == ("sea", "beach")

    async def test_at_most_six(self):
        photo = await self._labels(list(SCENE_LABELS[:10]))
        assert photo.scene_labels == SCENE_LABELS[:6]

    @pytest.mark.parametrize("raw", [None, "sunset", {"a": 1}, []])
    async def test_a_missing_or_malformed_list_is_no_labels_never_an_error(self, raw):
        assert (await self._labels(raw)).scene_labels == ()

    async def test_a_text_image_has_no_photo_so_no_labels(self):
        agent, _ = _agent([reply({"images": [_entry(1, "text", ["sunset"], capture="scan")]})])
        out = await agent.classify([_input(index=1)], account_id="a")
        assert out.results[1][1] is None
