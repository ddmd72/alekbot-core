"""Smart's propose_memory_facts tool — served locally by the DelegationEngine (MEMORY_FACT_CONFIRMATION_RFC)."""

from typing import Any, Awaitable, Callable, Dict

from pydantic import ValidationError

from ..domain.agent import DeliveryItem
from ..domain.exceptions import MemoryFactsRejected
from ..domain.llm import ToolCall
from ..domain.memory_fact import (
    MAX_FACT_CHARS,
    MAX_FACTS_PER_DRAFT,
    MAX_SOURCE_CHARS,
    MEMORY_PREVIEW_DELIVERY,
    PROPOSE_MEMORY_FACTS_TOOL,
    MemoryFactDraft,
    save_command,
)
from .delegation_engine import LocalToolHandler, ToolResult
from ..utils.logger import logger

_DESCRIPTION = (
    "Propose facts about the user's life for long-term memory; the owner confirms them. "
    "Memory is filled by background consolidation of this conversation: the user's own messages are "
    "picked up automatically — never propose what the user said themselves, including \"remember that I…\" — "
    "and your own replies are not a memory source. Facts you only read — a file, a drive document, an email, a "
    "search result — are not picked up. When the user asks you to remember facts from such a source, propose "
    "them here. The owner sees the list with a save command; nothing is stored until they paste it, and then "
    "the facts reach memory only after consolidation, usually hours later; search_memory cannot find them "
    "before that. Only facts about the user and their life: people, health, home, documents, dates, "
    "possessions, decisions. Never procedures or how-tos (propose a skill), rules for your own behaviour, "
    "task progress, or facts about the world."
)


def build_propose_memory_facts_tool_declaration() -> Dict[str, Any]:
    return {
        "name": PROPOSE_MEMORY_FACTS_TOOL,
        "description": _DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "description": f"The facts to propose, 1 to {MAX_FACTS_PER_DRAFT}.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": (
                                    "One self-contained fact in the third person ('User …' or 'User's mother …'), with "
                                    f"its date when known. One line, at most {MAX_FACT_CHARS} characters."
                                ),
                            },
                            "source": {
                                "type": "string",
                                "description": (
                                    "Where it comes from: the file name, drive path, email subject or URL. "
                                    f"One line, at most {MAX_SOURCE_CHARS} characters."
                                ),
                            },
                        },
                        "required": ["text", "source"],
                    },
                },
            },
            "required": ["facts"],
        },
    }


def make_propose_memory_facts_handler(
    propose: Callable[[MemoryFactDraft], Awaitable[str]],
) -> LocalToolHandler:
    """Build per execution: the closure binds the requesting user's id via `propose`."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        raw = (tool_call.args or {}).get("facts")
        try:
            draft = MemoryFactDraft.model_validate({"facts": raw if isinstance(raw, list) else []})
        except ValidationError as e:
            reason = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()
            ) or str(e)
            logger.info("🧠 [propose_memory_facts] validation rejected: %s", reason)
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: proposal rejected — {reason}. Fix it and call {PROPOSE_MEMORY_FACTS_TOOL} again.",
                failed=True,
            )

        try:
            code = await propose(draft)
        except MemoryFactsRejected as e:
            logger.info("🧠 [propose_memory_facts] rejected: %s", e)
            return ToolResult(
                name=tool_call.name,
                result_str=f"SYSTEM: proposal rejected — {e}. Leave that fact out or reword it from the source.",
                failed=True,
            )
        except Exception as e:
            logger.error("❌ [propose_memory_facts] proposal could not be stored: %s", e)
            return ToolResult(
                name=tool_call.name,
                result_str="SYSTEM: the proposal could not be stored; nothing was saved.",
                failed=True,
            )

        logger.info("🧠 [propose_memory_facts] proposed %d facts", len(draft.facts))
        return ToolResult(
            name=tool_call.name,
            result_str=(
                f"SYSTEM: Done. The proposal was created and the owner sees {len(draft.facts)} proposed "
                "facts with a save command. Your part is complete: do not call propose_memory_facts "
                "again for these facts. Call it again only if the owner asks for changes. The facts go "
                "to memory when the owner pastes the save command; that step is the owner's. Say in "
                "one sentence that pasting the command saves them; do not list the facts again."
            ),
            delivery_items=[
                DeliveryItem(
                    type=MEMORY_PREVIEW_DELIVERY,
                    data={
                        "facts": [f.model_dump() for f in draft.facts],
                        "command": save_command(code),
                    },
                )
            ],
        )

    return handle
