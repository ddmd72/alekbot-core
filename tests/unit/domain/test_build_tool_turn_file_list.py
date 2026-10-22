"""build_tool_turn takes one file dict, a list of them, or none (DRIVE_SPECIALIST_RFC §6: several vision files of one
drive answer reach the orchestrator in order, one file part each, right after their tool_response)."""
from src.domain.llm import LLMResponse, ToolCall, build_tool_turn

A = {"path": "/tmp/a.jpg", "mime_type": "image/jpeg"}
B = {"path": "/tmp/b.pdf", "mime_type": "application/pdf"}


def _parts(file_data):
    calls = [ToolCall(name="delegate_to_specialist", args={}, thought_signature="t1"),
             ToolCall(name="delegate_to_specialist", args={}, thought_signature="t2")]
    entries = [(calls[0], "drive answer", file_data), (calls[1], "other")]
    return build_tool_turn(LLMResponse(tool_calls=calls), entries)[1].parts


def _shape(parts):
    return [("resp", p.tool_response["tool_use_id"]) if p.tool_response else ("file", p.file_data["path"])
            for p in parts]


def test_one_dict_adds_one_file_part():
    assert _shape(_parts(A)) == [("resp", "t1"), ("file", "/tmp/a.jpg"), ("resp", "t2")]


def test_a_list_adds_one_file_part_per_file_in_order():
    assert _shape(_parts([A, B])) == [("resp", "t1"), ("file", "/tmp/a.jpg"), ("file", "/tmp/b.pdf"), ("resp", "t2")]


def test_none_and_an_empty_list_add_no_file_part():
    assert _shape(_parts(None)) == _shape(_parts([])) == [("resp", "t1"), ("resp", "t2")]
