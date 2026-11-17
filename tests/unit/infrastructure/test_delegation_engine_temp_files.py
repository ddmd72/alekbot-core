"""A native open's temp file (`file_data["path"]` of a tool result) is removed when the delegation run ends, on every
path: the model reads it on later turns of the same run, and nothing reads it after."""
import pytest

from src.domain.agent import AgentResponse
from src.domain.llm import MessagePart
from src.infrastructure.delegation_engine import DelegationEngine
from src.ports.llm_port import LLMRequest, LLMResponse, Message, ToolCall


class FileCoordinator:
    def __init__(self, tmp_path, as_list=False):
        self._tmp = tmp_path
        self._as_list = as_list

    def get_execution_mode(self, intent):
        return None

    async def handle_delegation(self, intent, query, context, calling_agent_id):
        path = self._tmp / "opened.pdf"
        path.write_bytes(b"%PDF")
        data = {"path": str(path), "mime_type": "application/pdf"}
        return AgentResponse.success(task_id="t", agent_id="a", result="attached",
                                     metadata={"file_data": [data] if self._as_list else data})


def _open_call():
    return ToolCall(name="delegate_to_specialist", args={"intent": "open_file", "query": "q"})


def _run(coordinator, call_llm):
    return DelegationEngine(coordinator).execute(
        call_llm=call_llm,
        base_request=LLMRequest(model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])]),
        context={}, max_turns=4, terminal_tool="deliver_response",
    )


def _files(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir())


@pytest.mark.parametrize("as_list", [False, True])
async def test_temp_file_is_gone_after_a_run_that_ends_with_text(tmp_path, as_list):
    turns = [LLMResponse(text="", tool_calls=[_open_call()]), LLMResponse(text="done")]
    seen = []

    async def call_llm(req, turn=0):
        seen.append(_files(tmp_path))
        return turns.pop(0)

    await _run(FileCoordinator(tmp_path, as_list), call_llm)
    assert seen == [[], ["opened.pdf"]]
    assert _files(tmp_path) == []


async def test_temp_file_is_gone_after_the_terminal_tool(tmp_path):
    turns = [LLMResponse(text="", tool_calls=[_open_call()]),
             LLMResponse(text="", tool_calls=[ToolCall(name="deliver_response", args={"response": "x"})])]

    async def call_llm(req, turn=0):
        return turns.pop(0)

    await _run(FileCoordinator(tmp_path), call_llm)
    assert _files(tmp_path) == []


async def test_temp_file_is_gone_when_the_next_model_call_raises(tmp_path):
    turns = [LLMResponse(text="", tool_calls=[_open_call()])]

    async def call_llm(req, turn=0):
        if turns:
            return turns.pop(0)
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        await _run(FileCoordinator(tmp_path), call_llm)
    assert _files(tmp_path) == []


async def test_an_already_removed_file_is_not_an_error(tmp_path):
    class Vanishing(FileCoordinator):
        async def handle_delegation(self, *a, **k):
            response = await super().handle_delegation(*a, **k)
            (self._tmp / "opened.pdf").unlink()
            return response

    turns = [LLMResponse(text="", tool_calls=[_open_call()]), LLMResponse(text="done")]

    async def call_llm(req, turn=0):
        return turns.pop(0)

    result = await _run(Vanishing(tmp_path), call_llm)
    assert result.text == "done"
