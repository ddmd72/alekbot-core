"""Execution taint: did outside content enter this model execution? (EXTERNAL_TASK_QUEUE_RFC §5.3, §6.1.)

A task created in an execution that read outside content may carry an instruction that content planted, so such a
task is held for the owner's `$task send` instead of being queued. This module is the pure rule; `TaintTracker`
(`infrastructure/taint_tracker.py`) applies it to one execution.

The rule fails safe. An execution is tainted when any of these entered it:
- a tool result holding an untrusted wrapper or an attached-file reminder (`has_untrusted_wrapper`);
- a file part, in a tool result or in the request;
- a wrapper or an attached-file reminder already in the request (history, the current message, static blocks);
- a saved model message of an earlier turn that was itself tainted (`Message.execution_tainted`): a turn that read a web
  page leaves its answer in history, and the next turn builds on that answer, so the taint travels with it. A record
  without the field reads as clean;
- a message the owner's channel got while a long turn ran (the same request-side check, applied to those messages);
- the result of any tool call that is not on the closed allowlist below. The allowlist is what matters: web search
  output is not wrapped at all, so a rule that looked only for wrappers would miss a web page.

The allowlist is the sources whose output is the owner's own data or this queue's own answers: a skill's text, a memory
search, the queue's list / cancel / create results and its worker list, and the workspace (a workspace file that holds a worker's result
carries a wrapper, which taints through the first bullet). `get_external_task` is deliberately absent: every read of a
result or a reason taints.

Public interface:
    TRUSTED_LOCAL_TOOLS, TRUSTED_DELEGATE_INTENTS
    call_is_trusted(name, args) -> bool
    text_taints(text) -> bool
    result_taints(name, args, result_text, has_file) -> bool
    part_taints(part) -> bool;  request_taints(messages, static_blocks) -> bool
    EXECUTION_TAINTED_KEY                         the `AgentResponse.metadata` key that carries the final flag
"""
import json
from typing import Any, Iterable, Mapping, Optional

from .external_task_texts import (
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_EXTERNAL_TASK_TOOL,
    LIST_EXTERNAL_TASKS_TOOL,
    LIST_EXTERNAL_WORKERS_TOOL,
)
from .llm import Message, MessagePart
from .skill import USE_SKILL_TOOL
from .untrusted_text import has_untrusted_wrapper
from .workspace import WORKSPACE_TOOL

# The engine's delegation tool and the memory search intent. Pinned against `agent_manifest.Intent.SEARCH_MEMORY` and
# `delegation_engine.DELEGATE_TOOL_NAME` by a test: a domain module cannot import them.
# The response metadata key `SmartResponseAgent` sets and `ConversationHandler` stores on the saved model message.
EXECUTION_TAINTED_KEY = "execution_tainted"

DELEGATE_TOOL = "delegate_to_specialist"
MEMORY_SEARCH_INTENT = "search_memory"

TRUSTED_LOCAL_TOOLS = frozenset({
    USE_SKILL_TOOL, LIST_EXTERNAL_TASKS_TOOL, CANCEL_EXTERNAL_TASK_TOOL, CREATE_EXTERNAL_TASK_TOOL, WORKSPACE_TOOL,
    LIST_EXTERNAL_WORKERS_TOOL,     # labels and descriptions are the owner's own words, written in the Cabinet
})
TRUSTED_DELEGATE_INTENTS = frozenset({MEMORY_SEARCH_INTENT})


def call_is_trusted(name: str, args: Optional[Mapping[str, Any]]) -> bool:
    """On the allowlist: a trusted local tool, or a delegation to a trusted intent."""
    if name in TRUSTED_LOCAL_TOOLS:
        return True
    if name == DELEGATE_TOOL:
        return (args or {}).get("intent") in TRUSTED_DELEGATE_INTENTS
    return False


def text_taints(text: Optional[str]) -> bool:
    return bool(text) and has_untrusted_wrapper(text or "")


def result_taints(name: str, args: Optional[Mapping[str, Any]], result_text: Optional[str], has_file: bool) -> bool:
    """One tool result: its source is off the allowlist, it carries a file, or its text holds a wrapper."""
    return has_file or not call_is_trusted(name, args) or text_taints(result_text)


def part_taints(part: MessagePart) -> bool:
    """One request part: a file part, or text (also a stored tool response) holding a wrapper."""
    if part.file_data:
        return True
    if text_taints(part.text) or text_taints(part.full_text):
        return True
    if part.tool_response is not None:
        return text_taints(json.dumps(part.tool_response, ensure_ascii=False, default=str))
    return False


def request_taints(messages: Iterable[Message], static_blocks: Iterable[str] = ()) -> bool:
    """The request before the first model call: history, the current message parts and the static blocks."""
    if any(text_taints(block) for block in static_blocks):
        return True
    return any(message.execution_tainted or any(part_taints(part) for part in message.parts) for message in messages)
