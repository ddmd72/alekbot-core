"""Several vision files after one tool_response, as build_tool_turn emits them for a drive answer (DRIVE_SPECIALIST_RFC
§6, docs/how_to/ADAPTER_WIRE_TESTING.md): no adapter change. Claude nests every file into the preceding tool_result
(the tool_result blocks stay contiguous); OpenAI sends the files as one content item after the function outputs;
Gemini sends one part per file; Grok sends the images and drops the PDF (today's behaviour). Mock boundary: the
adapters' message conversion, fed a real build_tool_turn history."""
import base64
from unittest.mock import MagicMock

from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.llm import LLMResponse, Message, MessagePart, ToolCall, build_tool_turn

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0 a jpeg").decode()
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n a png").decode()
PDF = base64.b64encode(b"%PDF-1.4 a pdf").decode()
FILES = [{"base64": JPEG, "mime_type": "image/jpeg"}, {"base64": PNG, "mime_type": "image/png"},
         {"base64": PDF, "mime_type": "application/pdf"}]


def _history(files=FILES):
    drive = ToolCall(name="delegate_to_specialist", args={"intent": "work_with_drive_files"}, thought_signature="call_A")
    web = ToolCall(name="delegate_to_specialist", args={"intent": "search_web"}, thought_signature="call_B")
    turn = build_tool_turn(LLMResponse(tool_calls=[drive, web]),
                           [(drive, "Drive result: ok", files), (web, "web result")])
    return [Message(role="user", parts=[MessagePart(text="show me")]), *turn]


async def test_claude_nests_every_file_into_its_tool_result():
    result = await ClaudeAdapter(api_key="k")._convert_messages(_history())
    content = result[-1]["content"]
    assert [b["type"] for b in content] == ["tool_result", "tool_result"]
    first, second = content
    assert first["tool_use_id"] == "call_A"
    assert [b["type"] for b in first["content"]] == ["text", "image", "image", "document"]
    assert second["tool_use_id"] == "call_B" and isinstance(second["content"], str)


async def test_openai_sends_the_files_after_the_function_outputs_in_order():
    items = await OpenAIAdapter(api_key="k")._convert_input(_history())
    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert len(outputs) == 2
    files = [i for i in items if i.get("role") == "user" and isinstance(i.get("content"), list)]
    assert len(files) == 1
    assert [p["type"] for p in files[0]["content"]] == ["input_image", "input_image", "input_file"]
    assert items.index(files[0]) > items.index(outputs[-1])


async def test_gemini_sends_one_part_per_file_after_the_function_response():
    adapter = GeminiAdapter(api_key="k")
    adapter.client = MagicMock()
    contents = await adapter._convert_messages(_history())
    parts = contents[-1].parts
    assert parts[0].function_response is not None and parts[4].function_response is not None
    assert [p.inline_data.mime_type for p in parts[1:4]] == ["image/jpeg", "image/png", "application/pdf"]
    adapter.client.files.upload.assert_not_called()


def test_grok_sends_the_images_and_drops_the_pdf():
    items = GrokAdapter(api_key="k")._convert_input(_history())
    files = [i for i in items if i.get("role") == "user" and isinstance(i.get("content"), list)]
    assert len(files) == 1
    assert [p["type"] for p in files[0]["content"]] == ["input_image", "input_image"]


# -- the production file shape: a path on disk, not base64 (what DriveOperations returns today) ------------------------

def _path_files(tmp_path):
    jpeg, pdf = tmp_path / "photo.jpg", tmp_path / "doc.pdf"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0 a jpeg")
    pdf.write_bytes(b"%PDF-1.4 a pdf")
    return [{"path": str(jpeg), "mime_type": "image/jpeg"}, {"path": str(pdf), "mime_type": "application/pdf"}]


async def test_claude_reads_path_files_into_the_tool_result(tmp_path):
    result = await ClaudeAdapter(api_key="k")._convert_messages(_history(_path_files(tmp_path)))
    first = result[-1]["content"][0]
    assert [b["type"] for b in first["content"]] == ["text", "image", "document"]
    assert first["content"][1]["source"]["data"] == base64.b64encode(b"\xff\xd8\xff\xe0 a jpeg").decode()


async def test_openai_reads_path_files_after_the_function_outputs(tmp_path):
    items = await OpenAIAdapter(api_key="k")._convert_input(_history(_path_files(tmp_path)))
    files = [i for i in items if i.get("role") == "user" and isinstance(i.get("content"), list)]
    assert len(files) == 1 and [p["type"] for p in files[0]["content"]] == ["input_image", "input_file"]


async def test_gemini_uploads_each_path_file_once_and_keeps_the_order(tmp_path):
    adapter = GeminiAdapter(api_key="k")
    adapter.client = MagicMock()
    adapter.client.files.upload.side_effect = lambda file, config: MagicMock(uri=f"files/{file[-3:]}",
                                                                              mime_type=config["mime_type"])
    contents = await adapter._convert_messages(_history(_path_files(tmp_path)))
    parts = contents[-1].parts
    assert [p.file_data.mime_type for p in parts[1:3]] == ["image/jpeg", "application/pdf"]
    assert adapter.client.files.upload.call_count == 2


def test_grok_reads_path_images_and_drops_the_pdf(tmp_path):
    items = GrokAdapter(api_key="k")._convert_input(_history(_path_files(tmp_path)))
    files = [i for i in items if i.get("role") == "user" and isinstance(i.get("content"), list)]
    assert len(files) == 1 and [p["type"] for p in files[0]["content"]] == ["input_image"]
