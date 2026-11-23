"""The execution taint rule (EXTERNAL_TASK_QUEUE_RFC §5.3): a closed allowlist of sources, wrappers and files."""
import pytest

from src.domain.execution_taint import (
    DELEGATE_TOOL,
    MEMORY_SEARCH_INTENT,
    TRUSTED_LOCAL_TOOLS,
    call_is_trusted,
    request_taints,
    result_taints,
)
from src.domain.external_task_texts import (
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_EXTERNAL_TASK_TOOL,
    GET_EXTERNAL_TASK_TOOL,
    LIST_EXTERNAL_TASKS_TOOL,
    LIST_EXTERNAL_WORKERS_TOOL,
)
from src.domain.llm import Message, MessagePart, ToolCall
from src.domain.untrusted_text import wrap_untrusted
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.delegation_engine import DELEGATE_TOOL_NAME

WRAPPED = wrap_untrusted("a page", nonce="abcdef0123456789", store="email", ref="r", label="l")


def test_the_pinned_names_are_the_engines_and_the_manifests():
    assert DELEGATE_TOOL == DELEGATE_TOOL_NAME
    assert MEMORY_SEARCH_INTENT == Intent.SEARCH_MEMORY


def test_the_allowlist_is_exactly_the_spec_and_get_is_not_on_it():
    assert TRUSTED_LOCAL_TOOLS == {"use_skill", "workspace", LIST_EXTERNAL_TASKS_TOOL, CANCEL_EXTERNAL_TASK_TOOL,
                                   CREATE_EXTERNAL_TASK_TOOL, LIST_EXTERNAL_WORKERS_TOOL}
    assert GET_EXTERNAL_TASK_TOOL not in TRUSTED_LOCAL_TOOLS


@pytest.mark.parametrize("name,args,trusted", [
    ("use_skill", {"name": "x"}, True),
    ("workspace", {"command": "view"}, True),
    (DELEGATE_TOOL, {"intent": "search_memory", "query": "q"}, True),
    (DELEGATE_TOOL, {"intent": "search_web", "query": "q"}, False),      # web output is not wrapped at all
    (DELEGATE_TOOL, {"intent": "search_emails"}, False),
    (DELEGATE_TOOL, {}, False),
    (DELEGATE_TOOL, None, False),
    (GET_EXTERNAL_TASK_TOOL, {"task_id": "t"}, False),
    ("some_future_tool", {}, False),
])
def test_call_is_trusted(name, args, trusted):
    assert call_is_trusted(name, args) is trusted


def test_a_trusted_source_with_a_clean_result_does_not_taint():
    assert result_taints("use_skill", {}, "the skill text", False) is False


@pytest.mark.parametrize("name,args,text,has_file", [
    ("use_skill", {}, f"Tool note: {WRAPPED}", False),                 # a wrapper inside a trusted source's result
    ("workspace", {}, WRAPPED, False),                                 # a flagged workspace file, read wrapped
    ("workspace", {}, "text", True),                                   # a file part
    (DELEGATE_TOOL, {"intent": "search_web"}, "plain web text", False),  # an off-allowlist source, text clean
])
def test_a_result_taints(name, args, text, has_file):
    assert result_taints(name, args, text, has_file) is True


def _msg(*parts: MessagePart) -> Message:
    return Message(role="user", parts=list(parts))


def test_a_clean_request_does_not_taint():
    history = [_msg(MessagePart(text="hello")), Message(role="model", parts=[MessagePart(text="hi")])]
    assert request_taints(history, ["a static block"]) is False


@pytest.mark.parametrize("part", [
    MessagePart(file_data={"uri": "gs://b/o", "mime_type": "application/pdf"}),       # a chat upload
    MessagePart(text=f"earlier: {WRAPPED}"),                                           # a wrapper already in history
    MessagePart(full_text=WRAPPED),
    MessagePart(tool_response={"name": "x", "response": {"result": WRAPPED}}),
])
def test_a_request_part_taints(part):
    assert request_taints([_msg(part)]) is True


def test_a_static_block_with_a_wrapper_taints():
    assert request_taints([_msg(MessagePart(text="hi"))], [f"email_for_triage {{\n{WRAPPED}\n}}"]) is True


def test_a_tool_call_part_alone_does_not_taint():
    assert request_taints([_msg(MessagePart(tool_call=ToolCall(name="use_skill", args={"name": "a"})))]) is False


def test_the_workers_list_is_trusted_and_a_flagged_model_message_taints_a_request():
    from src.domain.external_task_texts import LIST_EXTERNAL_WORKERS_TOOL
    assert call_is_trusted(LIST_EXTERNAL_WORKERS_TOOL, {}) is True
    flagged = Message(role="model", parts=[MessagePart(text="an answer")], execution_tainted=True)
    plain = Message(role="model", parts=[MessagePart(text="an answer")])
    assert request_taints([flagged]) is True and request_taints([plain]) is False
