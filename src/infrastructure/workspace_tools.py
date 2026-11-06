"""Smart's `workspace` tool — served locally by the DelegationEngine (docs/10_rfcs/AGENT_WORKSPACE_RFC.md §7).

Built per execution attempt (provider rotation re-enters Smart's `_run` with a fresh transcript, §10 row 11). The
handler holds the execution's session: the generations it has seen; one asyncio.Lock that every command takes as its
first await — `_execute_tool_calls` creates a batch's tasks in emission order and asyncio.Lock wakes waiters FIFO, so
a batch applies in the order the model wrote it; and the shielded mutations still finishing after their caller was
cancelled (turn-budget cut or message timeout, §10 row 3). A mutation's task records its generation, logs (through
WorkspaceService) and releases the lock itself. Nothing from the workspace enters session history: no
history_context is set (§11.5).
"""

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Set

from ..domain.llm import ToolCall
from ..domain.workspace import (
    COMMANDS,
    MAX_PATH_CHARS,
    MUTATING_COMMANDS,
    WORKSPACE_TOOL,
    WorkspaceCall,
    WorkspaceReferences,
    WorkspaceResult,
)
from ..utils.logger import logger
from .delegation_engine import LocalToolHandler, ToolResult

if TYPE_CHECKING:
    from ..services.workspace_service import WorkspaceService

_DESCRIPTION = (
    "Your workspace: files you keep for your own work between conversations and runs — a knowledge base you build "
    "and follow. Use it when the user asks you to, when an instruction you are following (a skill, a reminder) names "
    "a workspace file or asks you to keep state, or when a firing reminder carries a workspace file; not on your own "
    "initiative otherwise. How to use a file is up to that instruction; when it says nothing, use your judgement. "
    '"Keep state in the workspace" means: keep one file for it, view it at the start, update it before you finish. '
    "Folders: projects/ (one file per task or recurring job), references/ (where things live and how to find them), "
    "scratch/ (drafts; deleted 7 days after the last write), binary_files/ (the only place for binary files — PDF, "
    "images, scans: copy puts them there byte for byte; you cannot create or edit them and grep does not search "
    "them; look at one with open_file and the ref workspace:binary_files/<name>); text files go to the other folders "
    "(copy accepts a text file for any of them). Files may grow: add new "
    "entries with insert at insert_line -1 (the end; no view needed), newest last, and read the latest with a negative "
    "offset (-50 = the last 50 lines); change existing text with str_replace. Write a long document by creating it "
    "with the first part and adding the rest with insert -1. Facts about the user never go here: what "
    "the user says reaches memory by itself; facts you read in a file or other source go through "
    "propose_memory_facts, only when the user asks. Something to do at a given time is a reminder. Every change "
    "keeps the previous version: history shows who wrote each one, restore brings one back. If a view header says an "
    "earlier attempt of this same reminder fire wrote the file, its result may not have reached the user: view the "
    "generation it names and work from that. If a file a skill or reminder names is missing, view shows whether it "
    "was deleted or renamed: tell the user and offer to restore it; never recreate it silently. A file that never "
    "existed may be created. To see the structure, view with path \"\" shows the tree of the whole workspace and view "
    "projects/ just that folder (depth opens collapsed levels). To find text, grep (literal by default; regex: true "
    "for a regular expression), narrowed by path; the result says where it searched; start with files_only on a big workspace, then view the file with "
    "offset near the hit."
)


def build_workspace_tool_declaration() -> Dict[str, Any]:
    """Flat, liberal input (§7.2): only `command` is required, no unions, limits stated in prose and enforced in code
    (decisions/mcp_tool_schema_liberal_input.md)."""
    return {
        "name": WORKSPACE_TOOL,
        "description": _DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": list(COMMANDS),
                    "description": (
                        "view, create, str_replace, insert, delete, rename, copy, history, restore or grep. Parameters a command "
                        "does not use are ignored."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace path, e.g. projects/garden-shed.md: lowercase letters, digits, - _ . and /, starting "
                        "with projects/, references/, scratch/ or binary_files/, with a file extension, "
                        f"at most {MAX_PATH_CHARS} characters. For view, history and grep also a folder (projects/) or \"\" for the whole workspace. "
                        "For copy: the destination."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "view: first line to show, 1-based; negative counts from the end (-50 = the last 50 lines). Empty or 0 means 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "view: number of lines to show. Empty, 0 or negative means 2000.",
                },
                "depth": {
                    "type": "integer",
                    "description": "view of a folder or \"\": how many folder levels to open. Empty, 0 or negative means 3.",
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "grep: the text to search for, matched literally unless regex is true. With regex true, ^ and $ "
                        "match the start and end of a LINE; list markers, indentation and markup before the text count "
                        "as part of the line, so ^T1 will not match \"- T1\"."
                    ),
                },
                "context": {
                    "type": "integer",
                    "description": "grep: lines shown before and after each match; 0 shows only the matching lines. Empty means 1; at most 5.",
                },
                "ignore_case": {"type": "boolean", "description": "grep: ignore upper/lower case."},
                "files_only": {
                    "type": "boolean",
                    "description": "grep: list the files with their match counts instead of the matching lines.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "grep: read pattern as a regular expression (RE2: no backreferences or lookaround).",
                },
                "generation": {
                    "type": "string",
                    "description": (
                        "view: read that earlier version (read-only). restore: the version to bring back. Take it "
                        "from history or a view header."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "create: the full text of the file — text files only (.md .txt .csv .tsv .json .yaml .yml) "
                        "under projects/, references/ or scratch/, at most 3 MB."
                    ),
                },
                "old_str": {
                    "type": "string",
                    "description": (
                        "str_replace: the exact text to replace; it must occur exactly once in the file. Include "
                        "enough surrounding text to make it unique."
                    ),
                },
                "new_str": {"type": "string", "description": "str_replace: the replacement text (may be empty)."},
                "insert_line": {
                    "type": "integer",
                    "description": (
                        "insert: insert after this line; 0 = at the top, -1 = at the end. A line number from a view "
                        "or grep in this run."
                    ),
                },
                "insert_text": {"type": "string", "description": "insert: the lines to insert."},
                "new_path": {
                    "type": "string",
                    "description": (
                        "rename: the new path; it must not exist. A file in binary_files/ stays in binary_files/; "
                        "nothing else can move into binary_files/."
                    ),
                },
                "source": {
                    "type": "string",
                    "description": (
                        "copy: a chat file name from a [File: …] label, a delivered-document key, a drive file ref "
                        "exactly as the drive tool returned it, or a workspace path. A text file may be copied into "
                        "projects/, references/ or scratch/ (into projects/ to edit it later); a binary file only "
                        "into binary_files/, which holds binary files only."
                    ),
                },
            },
            "required": ["command"],
        },
    }


_TRUNCATED = (
    "Tool note: your arguments were cut off at the output limit; nothing was written. Create the file with the first part, "
    "then add the rest with insert at insert_line -1."
)


def _log_refusal(command: str, user_id: str, origin: str, fire_key: Optional[str], reason: str) -> None:
    """The handler's own refusals, in the WorkspaceService line format (§16); `fire=` is `<note_id>@<due_at>`."""
    loud = reason == "truncated" or command in MUTATING_COMMANDS
    (logger.warning if loud else logger.info)(
        "[Workspace] op=%s user=%s path=- origin=%s fire=%s had_history=- bytes=0 gen=- result=refused:%s ms=0",
        "_".join(command.split())[:40] or "-", user_id[:8], origin,
        (fire_key or "-").removeprefix("reminder:"), reason,
    )


class WorkspaceSession:
    """One execution attempt's state: generations seen, the command lock, shielded mutations still in flight."""

    def __init__(self) -> None:
        self.generations: Dict[str, int] = {}
        self.line_generations: Dict[str, int] = {}   # line numbers seen by grep; not "viewed" (D18)
        self.lock = asyncio.Lock()
        self.pending: Set["asyncio.Task[WorkspaceResult]"] = set()

    def apply_result(self, result: WorkspaceResult) -> None:
        self.apply(result.generations)
        self.line_generations.update(result.line_generations)

    def apply(self, updates: Mapping[str, Optional[int]]) -> None:
        for path, generation in updates.items():
            if generation is None:
                self.generations.pop(path, None)
                self.line_generations.pop(path, None)
            else:
                self.generations[path] = generation


def make_workspace_handler(
    service: "WorkspaceService",
    user_id: str,
    origin: str,
    fire_key: Optional[str],
    references: WorkspaceReferences,
    session: Optional[WorkspaceSession] = None,
) -> LocalToolHandler:
    """Build per execution attempt; `session` is injectable for tests only."""
    state = session if session is not None else WorkspaceSession()

    async def mutate(call: WorkspaceCall, command: str, args: Mapping[str, Any]) -> WorkspaceResult:
        try:
            result = await service.run(call, command, args)  # logs its own [Workspace] line
            state.apply_result(result)
            return result
        finally:
            state.lock.release()

    async def handle(tool_call: ToolCall) -> ToolResult:
        await state.lock.acquire()  # the first await of every command, reads included (§7.6)
        handed_over = False
        try:
            args: Dict[str, Any] = dict(tool_call.args or {})
            command = str(args.get("command") or "").strip()
            if args.get("_parse_error") == "truncated_json":
                # The OpenAI / Grok adapters replace arguments cut off at the output limit with this marker.
                _log_refusal("-", user_id, origin, fire_key, "truncated")
                return ToolResult(name=tool_call.name, result_str=_TRUNCATED, failed=True)
            if command not in COMMANDS:
                _log_refusal(command, user_id, origin, fire_key, "command")
                return ToolResult(
                    name=tool_call.name,
                    result_str=f"Tool note: unknown workspace command '{command}'; use one of: {', '.join(COMMANDS)}.",
                    failed=True,
                )
            call = WorkspaceCall(
                user_id=user_id, origin=origin, fire_key=fire_key, known=dict(state.generations),
                line_known=dict(state.line_generations),
                references=references,
            )
            if command in MUTATING_COMMANDS:
                task = asyncio.ensure_future(mutate(call, command, args))
                handed_over = True  # `mutate` releases the lock when the mutation has landed
                state.pending.add(task)
                task.add_done_callback(state.pending.discard)
                result = await asyncio.shield(task)
            else:
                result = await service.run(call, command, args)
                state.apply_result(result)
            return ToolResult(name=tool_call.name, result_str=result.text, failed=result.failed)
        finally:
            if not handed_over:
                state.lock.release()

    return handle
