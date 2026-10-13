"""Wire tests for the Search v2 drive agents (USER_DRIVE_INDEXING_RFC §25.4.4, docs/how_to/ADAPTER_WIRE_TESTING.md),
mocked at each SDK boundary: the profiler's, the passages agent's and triage v2's response schemas reach OpenAI
(`text.format` json_schema, strict off, `store=false`), Claude (`output_config.format`, strict-shaped: every object
closed, no unsupported keyword, no union to count against the grammar's limit) and Gemini (`response_json_schema`
unchanged) as the agent declares them; triage v2 keeps every "Image N" label right before its thumbnail."""
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.agents import drive_image_triage_agent, drive_passage_agent, drive_profiler_agent
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA, INLINE_FILE_BYTES_ARE_SENT

SCHEMAS = {"profiler": drive_profiler_agent._RESPONSE_SCHEMA, "passages": drive_passage_agent._RESPONSE_SCHEMA,
           "triage_v2": drive_image_triage_agent._RESPONSE_SCHEMA}
JPEG = b"\xff\xd8\xff\xe0 one"
PNG = b"\x89PNG\r\n\x1a\n two"


def _request(schema, parts=None, model="m"):
    return LLMRequest(model_name=model, system_instruction="s", response_mime_type="application/json",
                      response_schema=schema, thinking="low", max_tokens=4096, allow_provider_storage=False,
                      messages=[Message(role="user", parts=parts or [MessagePart(text="go")])])


def _triage_parts():
    parts = []
    for index, (data, mime) in enumerate([(JPEG, "image/jpeg"), (PNG, "image/png")], start=1):
        parts.append(MessagePart(text=f"Image {index}: camera metadata absent"))
        parts.append(MessagePart(file_data={"base64": base64.b64encode(data).decode("ascii"), "mime_type": mime}))
    parts.append(MessagePart(text="Classify each of the 2 images above."))
    return parts


# -- OpenAI (Responses API) ------------------------------------------------------------------------------------------

def _openai():
    resp = MagicMock(status="completed", output=[], output_text="{}")
    resp.usage = MagicMock(input_tokens=1, output_tokens=1, total_tokens=2,
                           input_tokens_details=MagicMock(cached_tokens=0, cache_write_tokens=0))
    adapter = OpenAIAdapter(api_key="k")
    adapter.client.responses.create = AsyncMock(return_value=resp)
    return adapter


@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_openai_gets_the_schema_unstrict_and_no_storage(name):
    adapter = _openai()
    await adapter.generate_content(_request(SCHEMAS[name], model="gpt-6-luna"))
    kwargs = adapter.client.responses.create.call_args.kwargs
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is False and fmt["schema"] == SCHEMAS[name]
    assert kwargs["store"] is False


async def test_openai_triage_labels_precede_their_images():
    adapter = _openai()
    await adapter.generate_content(_request(SCHEMAS["triage_v2"], _triage_parts(), model="gpt-6-luna"))
    content = adapter.client.responses.create.call_args.kwargs["input"][-1]["content"]
    kinds = [(c["type"], c.get("text", "")[:7]) for c in content]
    assert kinds == [("input_text", "Image 1"), ("input_image", ""), ("input_text", "Image 2"), ("input_image", ""),
                     ("input_text", "Classif")]
    assert content[1]["image_url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii")


# -- Claude (messages.stream) ----------------------------------------------------------------------------------------

def _claude():
    adapter = ClaudeAdapter(api_key="k")
    captured = {}
    block = MagicMock()
    block.type, block.text = "text", "{}"
    usage = MagicMock(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    stream = AsyncMock()
    stream.get_final_message = AsyncMock(return_value=MagicMock(content=[block], usage=usage,
                                                                stop_reason="end_turn"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stream)
    cm.__aexit__ = AsyncMock(return_value=None)

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return cm

    adapter.client.messages.stream = capturing_stream
    return adapter, captured


def _objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_claude_gets_a_strict_shaped_schema_with_the_same_fields_and_enums(name):
    adapter, captured = _claude()
    await adapter.generate_content(_request(SCHEMAS[name], model="claude-haiku-5-5"))
    sent = captured["output_config"]["format"]
    assert sent["type"] == "json_schema"
    schema = sent["schema"]
    assert all(obj.get("additionalProperties") is False for obj in _objects(schema))
    assert [o.get("required") for o in _objects(schema)] == [o.get("required") for o in _objects(SCHEMAS[name])]
    assert str(schema).count("'enum'") == str(SCHEMAS[name]).count("'enum'")
    assert "anyOf" not in str(schema) and "'null'" not in str(schema)   # no union toward the grammar's limit


async def test_claude_triage_labels_precede_their_images():
    adapter, captured = _claude()
    await adapter.generate_content(_request(SCHEMAS["triage_v2"], _triage_parts(), model="claude-haiku-5-5"))
    content = captured["messages"][-1]["content"]
    assert [c["type"] for c in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "Image 1: camera metadata absent" and content[3]["source"]["media_type"] == "image/png"


# -- Gemini (aio.models.generate_content) ----------------------------------------------------------------------------

def _gemini():
    adapter = GeminiAdapter(api_key="k")
    captured = {}
    part = MagicMock(text="{}", thought=False, function_call=None)
    candidate = MagicMock(grounding_metadata=None, finish_reason=None)
    candidate.content.parts = [part]
    response = MagicMock(candidates=[candidate])
    response.usage_metadata = MagicMock(prompt_token_count=1, candidates_token_count=1, total_token_count=2,
                                        cached_content_token_count=0, thoughts_token_count=0)

    async def generate(model=None, contents=None, config=None):
        captured.update(model=model, contents=contents, config=config)
        return response

    adapter.client = MagicMock()
    adapter.client.aio.models.generate_content = generate
    return adapter, captured


@pytest.mark.parametrize("name", sorted(SCHEMAS))
async def test_gemini_gets_the_schema_as_json_schema(name):
    adapter, captured = _gemini()
    await adapter.generate_content(_request(SCHEMAS[name], model="gemini-3.8-flash"))
    DICT_SCHEMA_IS_SENT_AS_JSON_SCHEMA.validate("gemini", {**captured, "expected_schema": SCHEMAS[name]})
    assert captured["config"].tools is None


async def test_gemini_triage_labels_precede_their_images():
    adapter, captured = _gemini()
    await adapter.generate_content(_request(SCHEMAS["triage_v2"], _triage_parts(), model="gemini-3.8-flash"))
    sent = captured["contents"][0].parts
    assert [p.text for p in sent[0:4:2]] == ["Image 1: camera metadata absent", "Image 2: camera metadata absent"]
    INLINE_FILE_BYTES_ARE_SENT.validate("gemini", {**captured, "expected": [(JPEG, "image/jpeg"),
                                                                             (PNG, "image/png")]})
