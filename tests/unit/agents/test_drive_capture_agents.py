"""The reader and triage say how a file was captured (USER_DRIVE_INDEXING_RFC §26.3, D34), and the reader's brief has
no doc type any more (Δ64): schema and parse. A capture outside the four values, or missing, is no capture: it never
costs the transcription or the verdict (as a bad brief date does not)."""
import pytest

from src.agents.drive_document_reader_agent import (
    AGENT_TYPE as READER_TYPE, DriveDocumentReaderAgent, _RESPONSE_SCHEMA as READER_SCHEMA,
)
from src.agents.drive_image_triage_agent import (
    AGENT_TYPE as TRIAGE_TYPE, DriveImageTriageAgent, TriageInput, _RESPONSE_SCHEMA as TRIAGE_SCHEMA,
)
from src.domain.agent import AgentConfig
from src.domain.drive_index import TriageVerdict
from tests.unit.agents.drive_model_fakes import ScriptedLLM, context, prompt_builder, reply

CAPTURES = ["scan", "document_photo", "screenshot", "digital"]
BRIEF = {"description": "Residence card", "doc_date": "2026-01-31", "parties": ["A"], "amount": None}


def _reader(script):
    llm = ScriptedLLM(script)
    return DriveDocumentReaderAgent(AgentConfig(agent_id="r", agent_type=READER_TYPE), context(llm),
                                    prompt_builder()), llm


async def _read(agent, first=True):
    return await agent.read(data=b"%PDF-1.4", mime_type="application/pdf", file_name="window-001.pdf",
                            first_window=first, account_id="acc")


class TestReaderSchema:
    def test_capture_is_required_with_the_four_values(self):
        assert READER_SCHEMA["required"] == ["text", "capture", "brief"]
        assert READER_SCHEMA["properties"]["capture"]["enum"] == CAPTURES

    def test_the_brief_has_no_doc_type(self):
        brief = READER_SCHEMA["properties"]["brief"]
        assert "doc_type" not in brief["properties"] and "doc_type" not in brief["required"]
        assert brief["required"] == ["description", "doc_date", "parties", "amount"]


class TestReaderParse:
    async def test_capture_on_every_window(self):
        agent, _ = _reader([reply({"text": "x", "capture": "scan", "brief": BRIEF}),
                            reply({"text": "y", "capture": "screenshot", "brief": None})])
        assert (await _read(agent)).capture == "scan"
        assert (await _read(agent, first=False)).capture == "screenshot"

    @pytest.mark.parametrize("value", ["photo", "", None, 7])
    async def test_a_value_outside_the_set_is_no_capture_and_no_retry(self, value):
        agent, llm = _reader([reply({"text": "x", "capture": value, "brief": None})])
        out = await _read(agent, first=False)
        assert out.capture == "" and out.text == "x" and len(llm.requests) == 1

    async def test_a_missing_capture_is_no_capture(self):
        agent, llm = _reader([reply({"text": "x", "brief": None})])
        assert (await _read(agent, first=False)).capture == "" and len(llm.requests) == 1

    async def test_a_doc_type_the_model_still_sends_is_ignored(self):
        agent, _ = _reader([reply({"text": "x", "capture": "scan", "brief": {**BRIEF, "doc_type": "certificate"}})])
        out = await _read(agent)
        assert out.brief.doc_type == "" and out.brief.description == "Residence card"


def _triage(script):
    llm = ScriptedLLM(script)
    return DriveImageTriageAgent(AgentConfig(agent_id="t", agent_type=TRIAGE_TYPE), context(llm),
                                 prompt_builder()), llm


def _images(n):
    return [TriageInput(index=i, data=bytes([i]) * 3, mime_type="image/jpeg", camera_prior=False)
            for i in range(1, n + 1)]


def _entry(i, verdict, capture):
    return {"index": i, "verdict": verdict, "capture": capture, "description": "a dog" if verdict == "photo" else "",
            "objects": [], "scene": "", "visible_text": ""}


class TestTriage:
    def test_the_schema_asks_every_image_for_its_capture(self):
        item = TRIAGE_SCHEMA["properties"]["images"]["items"]
        assert "capture" in item["required"]
        assert item["properties"]["capture"]["enum"] == CAPTURES + ["none"]

    async def test_a_text_image_carries_its_capture_and_a_photo_none(self):
        agent, _ = _triage([reply({"images": [_entry(1, "text", "screenshot"), _entry(2, "photo", "none")]})])
        answer = await agent.classify(_images(2), account_id="acc")
        assert answer.results[1][0] is TriageVerdict.TEXT and answer.captures == {1: "screenshot"}

    async def test_a_photo_never_gets_a_capture_whatever_the_model_says(self):
        agent, _ = _triage([reply({"images": [_entry(1, "photo", "scan")]})])
        assert (await agent.classify(_images(1), account_id="acc")).captures == {}

    async def test_a_bad_or_missing_capture_on_a_text_image_is_none(self):
        bad = _entry(1, "text", "fax")
        missing = {k: v for k, v in _entry(2, "text", "scan").items() if k != "capture"}
        agent, llm = _triage([reply({"images": [bad, missing]})])
        answer = await agent.classify(_images(2), account_id="acc")
        assert set(answer.results) == {1, 2} and answer.captures == {} and len(llm.requests) == 1


    async def test_a_shifted_twin_pair_keeps_no_capture(self):
        # two identical images with different verdicts are a shifted answer: dropped, asked again; twins that still
        # disagree are settled as text WITHOUT the capture said in the shifted answer
        twin = bytes([9]) * 3
        images = [TriageInput(index=i, data=twin, mime_type="image/jpeg", camera_prior=False) for i in (1, 2)]
        shifted = {"images": [_entry(1, "text", "scan"), _entry(2, "photo", "none")]}
        agent, _ = _triage([reply(shifted), reply(shifted)])
        answer = await agent.classify(images, account_id="acc")
        assert all(v[0] is TriageVerdict.TEXT for v in answer.results.values()) and answer.captures == {}
