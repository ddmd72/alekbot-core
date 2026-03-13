"""
DocGeneratorAgent
=================

Internal specialist agent: receives the raw JSON layout spec from DocPlannerAgent,
writes a Node.js script using the docx npm library, executes it via DocxRunnerPort,
and returns the DOCX bytes as a delivery_items file_upload.

Registration: internal=True — never exposed to LLMs. Dispatched by DocPlannerAgent
as a separate ASYNC Cloud Task (fire and forget) via coordinator.handle_delegation(
Intent.GENERATE_DOCX_CODE, raw_query, ...).

payload["query"] is the raw LLM text output from DocPlannerAgent (JSON string).
It is forwarded as-is to the LLM user message and piped to the Node.js script stdin.

Retry loop (max MAX_TURNS total LLM calls):
  - LLM writes script and calls the generate_docx tool.
  - Runner executes the Node.js script via DocxRunnerPort.
  - On tool success: DOCX bytes captured, agent returns immediately.
  - On tool error: stderr appended as tool response, LLM retries.
  - On LLM finishing without tool call: DocGeneratorError raised → failure response.
  - On MAX_TURNS exhausted: failure response.

Result delivered via DeliveryItem("file_upload", {...}).
AgentWorkerHandler calls notification_service.notify_file_bytes() on completion.
"""

import base64
import json
import time
from datetime import date
from typing import List, Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import DOC_GENERATOR
from ..ports.docx_runner_port import DocxRunnerError, DocxRunnerPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger


_GENERATE_DOCX_TOOL: List[dict] = [
    {
        "name": "generate_docx",
        "description": (
            "Execute a Node.js script that generates a DOCX file using the docx npm library. "
            "The script reads the layout spec from process.stdin and writes raw DOCX bytes to process.stdout. "
            "Returns {status: 'success', bytes_size: N} on success or {status: 'error', stderr: '...'} on failure. "
            "Call this tool again with a corrected script if execution fails."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "js_code": {
                    "type": "string",
                    "description": (
                        "Complete, executable Node.js script using the docx npm library. "
                        "Must read doc_spec JSON from process.stdin and write raw DOCX bytes to process.stdout."
                    ),
                }
            },
            "required": ["js_code"],
        },
    }
]


class DocGeneratorError(Exception):
    """Raised when DOCX generation fails after all turns."""


class DocGeneratorAgent(BaseAgent):
    """
    Specialist agent: LLM writes a Node.js docx script, runner executes it.

    Receives the JSON layout spec via payload["query"] (JSON string from coordinator).
    Returns AgentResponse with delivery_items=[DeliveryItem("file_upload", {...})] on success.
    Returns AgentResponse.failure(...) on all failure paths.
    """

    MAX_TURNS = 5
    TEMPERATURE = DOC_GENERATOR.temperature
    MAX_TOKENS = DOC_GENERATOR.max_tokens
    THINKING_EFFORT = DOC_GENERATOR.thinking_effort
    NODE_TIMEOUT = DOC_GENERATOR.node_timeout_s

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        docx_runner: DocxRunnerPort,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._runner = docx_runner
        self.prompt_builder = prompt_builder
        self.user_id = user_id

    async def can_handle(self, message: AgentMessage) -> bool:
        return (
            message.intent in (AgentIntent.QUERY, AgentIntent.DELEGATE)
            and bool(message.payload.get("query"))
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        raw_query = message.payload.get("query", "")
        if not raw_query:
            self._on_agent_error(ValueError("No spec provided"), "empty_query")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No spec provided in payload",
            )

        account_id = message.context.get("account_id")
        self._on_agent_start("generate_docx")
        start_time = time.time()

        try:
            system_prompt = await self._build_system_prompt(account_id)
        except Exception as exc:
            self._on_agent_error(exc, "prompt_builder")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Failed to build system prompt: {exc}",
            )

        messages = [
            Message(
                role="user",
                parts=[MessagePart(
                    text=(
                        "Implement the document layout specification as a Node.js script "
                        "and call generate_docx to produce the DOCX file.\n\n"
                        f"{raw_query}"
                    )
                )],
            )
        ]

        captured_bytes: Optional[bytes] = None

        for turn in range(self.MAX_TURNS):
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                tools=_GENERATE_DOCX_TOOL,
                thinking=self.THINKING_EFFORT or None,
                force_tool_use=True,
            )
            response = await self._call_llm(request, turn=turn)

            if not response.tool_calls:
                if captured_bytes:
                    break
                err = DocGeneratorError("LLM finished without calling generate_docx")
                self._on_agent_error(err, "docx_generation")
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="LLM finished without calling generate_docx — no DOCX produced",
                )

            # Append model message with tool calls
            messages = messages + [
                Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
                )
            ]

            tool_result_parts = []
            for tc in response.tool_calls:
                if tc.name != "generate_docx":
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "stderr": f"Unknown tool: {tc.name}"},
                    }))
                    continue

                js_code = (tc.args or {}).get("js_code", "")
                if not js_code:
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "stderr": "js_code argument is empty"},
                    }))
                    continue

                try:
                    docx_bytes = await self._runner.run(js_code, raw_query, self.NODE_TIMEOUT)
                    captured_bytes = docx_bytes
                    logger.info(
                        "DocGeneratorAgent: turn %d — generate_docx success, %d bytes",
                        turn + 1, len(docx_bytes),
                    )
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "success", "bytes_size": len(docx_bytes)},
                    }))
                except DocxRunnerError as exc:
                    logger.warning(
                        "DocGeneratorAgent: turn %d — generate_docx error: %s",
                        turn + 1, exc,
                    )
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "stderr": str(exc)},
                    }))

            messages = messages + [
                Message(role="user", parts=tool_result_parts)
            ]

            if captured_bytes:
                token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
                duration_ms = int((time.time() - start_time) * 1000)
                self._on_agent_success(len(captured_bytes), token_count, output_text="docx_generated")

                try:
                    _parsed = json.loads(raw_query)
                    doc_spec = _parsed.get("doc_spec", {}) if isinstance(_parsed, dict) else {}
                except Exception:
                    doc_spec = {}
                filename = _make_filename(doc_spec)
                title = doc_spec.get("title") or doc_spec.get("document_type", "Document").capitalize()

                return AgentResponse.success(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    result="docx_generated",
                    confidence=1.0,
                    metadata={"duration_ms": duration_ms, "model": self.model_name},
                    delivery_items=[
                        DeliveryItem(
                            type="file_upload",
                            data={
                                "file_bytes_b64": base64.b64encode(captured_bytes).decode("utf-8"),
                                "filename": filename,
                                "title": title,
                            },
                        )
                    ],
                )

        self._on_agent_error(RuntimeError("MAX_TURNS exhausted"), "docx_generation")
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=f"DOCX generation failed after {self.MAX_TURNS} turns without a successful generate_docx call",
        )

    async def _build_system_prompt(self, account_id: Optional[str]) -> str:
        return await self.prompt_builder.build_for_agent(
            agent_type="doc_generator",
            user_id=self.user_id,
            account_id=account_id,
            routing_metadata=None,
            include_biographical=False,
        )

    def _get_alternative_agents(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Filename helper
# ---------------------------------------------------------------------------

def _make_filename(doc_spec: dict) -> str:
    doc_type = (
        doc_spec.get("document_type", "document")
        .lower()
        .replace(" ", "-")
        .replace("/", "-")
    )
    today = date.today().strftime("%Y-%m-%d")
    return f"{doc_type}-{today}.docx"
