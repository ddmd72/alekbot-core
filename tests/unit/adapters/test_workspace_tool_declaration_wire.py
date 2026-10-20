"""Smart's full local-tool list, workspace included, crosses every provider adapter intact (AGENT_WORKSPACE_RFC §17;
docs/how_to/ADAPTER_WIRE_TESTING.md): names in order, the command enum, one required field; strict:false on OpenAI and Grok; the integer
window param (offset) is checked on Gemini only."""
from src.adapters.claude_adapter import ClaudeAdapter
from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.workspace import COMMANDS
from src.infrastructure.memory_fact_tools import build_propose_memory_facts_tool_declaration
from src.infrastructure.skill_tools import build_use_skill_tool_declaration
from src.infrastructure.workspace_tools import build_workspace_tool_declaration

_DELEGATE = {"name": "delegate_to_specialist", "description": "d",
             "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}}
_TOOLS = [_DELEGATE, build_use_skill_tool_declaration(), build_propose_memory_facts_tool_declaration(),
          build_workspace_tool_declaration()]
_NAMES = ["delegate_to_specialist", "use_skill", "propose_memory_facts", "workspace"]


def test_claude():
    out = ClaudeAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES
    schema = out[3]["input_schema"]
    assert schema["properties"]["command"]["enum"] == list(COMMANDS) and schema["required"] == ["command"]


def test_openai_keeps_strict_off():
    out = OpenAIAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES
    ws = out[3]
    assert ws["strict"] is False
    assert ws["parameters"]["properties"]["command"]["enum"] == list(COMMANDS)


def test_grok():
    out = GrokAdapter(api_key="k")._convert_tools(_TOOLS)
    assert [t["name"] for t in out] == _NAMES
    assert out[3]["strict"] is False
    assert out[3]["parameters"]["properties"]["command"]["enum"] == list(COMMANDS)
    assert out[3]["parameters"]["required"] == ["command"]


def test_gemini():
    [tool] = GeminiAdapter(api_key="k")._convert_tools_to_sdk_format(_TOOLS)
    assert [f.name for f in tool.function_declarations] == _NAMES
    params = tool.function_declarations[3].parameters
    assert params.properties["command"].enum == list(COMMANDS)
    assert params.required == ["command"]
    assert params.properties["offset"].type.name == "INTEGER"
