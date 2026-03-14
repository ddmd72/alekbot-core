"""
PdfGeneratorAgent
=================

Internal specialist agent: receives the raw JSON layout spec from PdfPlannerAgent,
instructs the LLM to write a complete HTML+CSS document, renders it to PDF via
PuppeteerRunnerPort, and returns both HTML and PDF as "document" DeliveryItems.

Registration: internal=True — never exposed to LLMs. Dispatched by PdfPlannerAgent
as a separate ASYNC Cloud Task via coordinator.handle_delegation(
Intent.GENERATE_PDF_CODE, raw_query, ...).

payload["query"] is the raw LLM JSON output from PdfPlannerAgent (JSON string).

Delivery:
  DeliveryItem #1: HTML → GCS → named link in Slack
  DeliveryItem #2: PDF  → GCS → named link + Slack file upload

Retry loop (max MAX_TURNS total LLM calls):
  - LLM writes HTML and calls the generate_html tool.
  - Runner renders HTML to PDF via Puppeteer.
  - On tool success: both HTML and PDF bytes captured, agent returns immediately.
  - On tool error: error appended as tool response, LLM retries.
  - On LLM finishing without tool call: PdfGeneratorError raised → failure response.
  - On MAX_TURNS exhausted: failure response.
"""

import base64
import json
import time
from typing import List, Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import PDF_GENERATOR
from ..ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger


_GENERATE_HTML_TOOL: List[dict] = [
    {
        "name": "generate_html",
        "description": (
            "Submit a complete HTML+CSS document for PDF rendering via Puppeteer. "
            "The document must be fully self-contained: all CSS embedded in <style> tags, "
            "no external stylesheets, no external fonts, no JavaScript. "
            "Use @page CSS rules to control page size and margins. "
            "Returns {status: 'success', pdf_size: N, html_size: N} on success "
            "or {status: 'error', message: '...'} on failure. "
            "Call this tool again with a corrected document if rendering fails."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "html_code": {
                    "type": "string",
                    "description": (
                        "Complete HTML5 document with embedded CSS. "
                        "Must be self-contained and print-ready. "
                        "Use @page for page setup and page-break-before for section breaks."
                    ),
                }
            },
            "required": ["html_code"],
        },
    }
]


class PdfGeneratorError(Exception):
    """Raised when PDF generation fails after all turns."""


class PdfGeneratorAgent(BaseAgent):
    """
    Specialist agent: LLM writes HTML+CSS, Puppeteer renders PDF.

    Receives the JSON layout spec via payload["query"] (JSON string from coordinator).
    Returns AgentResponse with two delivery_items on success:
      - HTML "document" (GCS link)
      - PDF  "document" (GCS link + Slack file upload)
    """

    MAX_TURNS = 5
    TEMPERATURE = PDF_GENERATOR.temperature
    MAX_TOKENS = PDF_GENERATOR.max_tokens
    THINKING_EFFORT = PDF_GENERATOR.thinking_effort
    NODE_TIMEOUT = PDF_GENERATOR.node_timeout_s

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        pdf_runner: PuppeteerRunnerPort,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._runner = pdf_runner
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
        self._on_agent_start("generate_pdf")
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
                        "Implement the PDF layout specification as a complete HTML+CSS document "
                        "and call generate_html to submit it for rendering.\n\n"
                        f"{raw_query}"
                    )
                )],
            )
        ]

        captured_html: Optional[str] = None
        captured_pdf: Optional[bytes] = None

        for turn in range(self.MAX_TURNS):
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                tools=_GENERATE_HTML_TOOL,
                thinking=self.THINKING_EFFORT or None,
                force_tool_use=True,
            )
            response = await self._call_llm(request, turn=turn)

            if not response.tool_calls:
                if captured_pdf:
                    break
                err = PdfGeneratorError("LLM finished without calling generate_html")
                self._on_agent_error(err, "pdf_generation")
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="LLM finished without calling generate_html — no PDF produced",
                )

            messages = messages + [
                Message(
                    role="model",
                    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
                )
            ]

            tool_result_parts = []
            for tc in response.tool_calls:
                if tc.name != "generate_html":
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "message": f"Unknown tool: {tc.name}"},
                    }))
                    continue

                html_code = (tc.args or {}).get("html_code", "")
                if not html_code:
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "message": "html_code argument is empty"},
                    }))
                    continue

                try:
                    pdf_bytes = await self._runner.run(html_code, self.NODE_TIMEOUT)
                    captured_html = html_code
                    captured_pdf = pdf_bytes
                    logger.info(
                        "PdfGeneratorAgent: turn %d — generate_html success, pdf=%d bytes html=%d bytes",
                        turn + 1, len(pdf_bytes), len(html_code),
                    )
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {
                            "status": "success",
                            "pdf_size": len(pdf_bytes),
                            "html_size": len(html_code),
                        },
                    }))
                except PuppeteerRunnerError as exc:
                    logger.warning(
                        "PdfGeneratorAgent: turn %d — generate_html error: %s",
                        turn + 1, exc,
                    )
                    tool_result_parts.append(MessagePart(tool_response={
                        "name": tc.name,
                        "response": {"status": "error", "message": str(exc)},
                    }))

            messages = messages + [
                Message(role="user", parts=tool_result_parts)
            ]

            if captured_pdf and captured_html:
                token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
                duration_ms = int((time.time() - start_time) * 1000)
                self._on_agent_success(
                    len(captured_pdf), token_count, output_text="pdf_generated"
                )

                try:
                    _parsed = json.loads(raw_query)
                    doc_spec = _parsed.get("doc_spec", {}) if isinstance(_parsed, dict) else {}
                except Exception:
                    doc_spec = {}

                base_filename = _extract_filename(doc_spec)
                display_name = doc_spec.get("title") or doc_spec.get("document_type", "Document").capitalize()

                return AgentResponse.success(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    result="pdf_generated",
                    confidence=1.0,
                    metadata={"duration_ms": duration_ms, "model": self.model_name},
                    delivery_items=[
                        DeliveryItem(
                            type="document",
                            data={
                                "content_b64": base64.b64encode(captured_html.encode("utf-8")).decode("utf-8"),
                                "filename": f"{base_filename}.html",
                                "content_type": "text/html; charset=utf-8",
                                "label": f"{display_name}.html",
                                "file_upload": False,
                            },
                        ),
                        DeliveryItem(
                            type="document",
                            data={
                                "content_b64": base64.b64encode(captured_pdf).decode("utf-8"),
                                "filename": f"{base_filename}.pdf",
                                "content_type": "application/pdf",
                                "label": f"{display_name}.pdf",
                                "file_upload": True,
                            },
                        ),
                    ],
                )

        self._on_agent_error(RuntimeError("MAX_TURNS exhausted"), "pdf_generation")
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=f"PDF generation failed after {self.MAX_TURNS} turns without a successful generate_html call",
        )

    async def _build_system_prompt(self, account_id: Optional[str]) -> str:
        return await self.prompt_builder.build_for_agent(
            agent_type="pdf_generator",
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

def _extract_filename(doc_spec: dict) -> str:
    """Extract or derive a short base filename (no extension) from the spec."""
    filename = doc_spec.get("filename", "").strip()
    if filename:
        # Sanitize: keep alphanumerics, underscores, hyphens
        return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in filename)
    # Fallback: derive from document_type
    doc_type = (
        doc_spec.get("document_type", "document")
        .lower()
        .replace(" ", "_")
        .replace("/", "_")
    )
    return doc_type
