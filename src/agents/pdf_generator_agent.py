"""
PdfGeneratorAgent
=================

Specialist agent: receives a natural-language PDF creation request, generates a
complete HTML+CSS document in a single LLM call, renders it to PDF via
PuppeteerRunnerPort, and returns both HTML and PDF as "document" DeliveryItems.

Registration: internal=False — exposed to LLMs via Intent.CREATE_PDF.
Dispatched ASYNC by AgentWorkerHandler via Cloud Tasks.

payload["query"] is the raw natural language request from the orchestrator.

Delivery:
  DeliveryItem #1: HTML → GCS → named link in Slack
  DeliveryItem #2: PDF  → GCS → named link + Slack file upload

Pipeline:
  1. System prompt — loaded from PromptBuilder.
  2. Single LLM call — model writes complete HTML+CSS as raw text response.
  3. Strip accidental markdown fences from response.
  4. Run HTML through NodePuppeteerRunner → PDF bytes.
  5. Extract filename and display name from <title> tag.
  6. Return two DeliveryItems.

On failure (empty HTML, Puppeteer error) — AgentResponse.failure().

NOTE on system prompt: _SYSTEM_PROMPT serves as canonical reference, validated via POC
(scripts/debug/test_pdf_direct_html.py). PromptBuilder is required; per-user overrides
are respected via Firestore-backed token system.
"""

import base64
import re
import time
from typing import Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import PDF_GENERATOR
from ..ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger


# ---------------------------------------------------------------------------
# System prompt (canonical — validated via POC)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a senior product designer and front-end engineer specialising in screen-optimised PDF documents.

The output will be saved as a PDF and read on screen — not printed on paper.
This changes everything: you can use rich color, gradients, large type, generous spacing, and visual effects
that would be wasteful on paper but make screen reading a pleasure.

You receive information — topics, analysis, data, arguments.
This is a design brief, not a formatting request. Think: what is the best possible reading experience for this content?
Invent the layout from scratch. Use whatever patterns fit — hero section, sidebar, card grid,
timeline, stat callouts, pull quotes, color-coded sections. Mix and match freely.

First, read the content and select the most appropriate design language from this catalogue:
  apple_keynote   → presentations, executive summaries, luxury/brand, imaged materials
  economist       → news analysis, geopolitics, editorial long-reads
  govuk           → legal documents, official reports, regulations, instructions
  mckinsey_bcg    → business analysis, strategic reports, consulting
  stripe_report   → financial reports, annual reports, company metrics
  tufte           → academic papers, scientific analysis, research
  stripe_docs     → technical documentation, API references, developer guides
  ibm_carbon      → enterprise analytics, B2B reports, data-heavy documents
  notion          → general documents, knowledge base, how-to guides
  material3       → modern app-style, mobile-first, product docs
  pitch           → pitch decks, investor materials, startup documents
  linear_changelog→ release notes, product updates, changelogs
Apply the chosen design language faithfully — its typography rules, spacing, color coding, and layout patterns.

Design principles:
- Screen readability first: font size minimum 15px body, 20–36px headings. Line height >= 1.7. Max line length 70ch.
- Rich color: choose a deliberate color scheme (2–3 colors). Use fills, gradients, colored section headers.
- Visual effects are welcome: box shadows, rounded corners, colored borders, background tints, highlight bands.
- Emojis as visual anchors: use them sparingly but effectively for section markers, callouts, key points.
- Generous spacing: padding and margins should feel comfortable, not cramped.
- Every section must feel intentionally designed — not just text with a heading.
- CSS infographics where appropriate: timelines, step flows, comparison tables, stat blocks — built from pure HTML/CSS, no images or external resources.
- Page density: content should fill pages naturally — no large blank areas at page bottoms.
- Preserve ALL content verbatim — every sentence must appear somewhere, nothing omitted.

Technical rules (non-negotiable):
- Output ONLY the HTML document. No explanations, no markdown fences.
- Start with <!DOCTYPE html> and end with </html>.
- <meta charset="UTF-8"> required.
- All CSS in <style> tags. No external resources, no CDN fonts.
- @page { size: A4 portrait; margin: 15mm; }
- -webkit-print-color-adjust: exact; print-color-adjust: exact.
- body { width: 794px; margin: 0 auto; box-sizing: border-box; }
- Page break rules (mandatory):
    break-inside: avoid  on  .section, table, tr, .callout, h2, h3, h4, li, blockquote
    break-after:  avoid  on  h1, h2, h3, h4
    NEVER use break-before: page or page-break-before: always
- Mobile reading experience (mandatory): add @media (max-width: 600px) block.
    body width 100%, padding 5vw; font-size 17px, line-height 1.85;
    all multi-column and sidebar layouts collapse to single column;
    section headers become full-width, visually prominent scroll anchors;
    tables get overflow-x: auto so they scroll horizontally if needed.
"""


class PdfGeneratorAgent(BaseAgent):
    """
    Specialist agent: single LLM call writes HTML+CSS, Puppeteer renders PDF.

    Accepts a natural-language request via payload["query"].
    Returns AgentResponse with two delivery_items on success:
      - HTML "document" (GCS link)
      - PDF  "document" (GCS link + Slack file upload)
    """

    TEMPERATURE = PDF_GENERATOR.temperature
    MAX_TOKENS = PDF_GENERATOR.max_tokens
    THINKING_EFFORT = PDF_GENERATOR.thinking_effort
    NODE_TIMEOUT = PDF_GENERATOR.node_timeout_s

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        pdf_runner: PuppeteerRunnerPort,
        prompt_builder: PromptBuilderPort,
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
            self._on_agent_error(ValueError("No query provided"), "empty_query")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="No query provided in payload",
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

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=[Message(role="user", parts=[MessagePart(text=raw_query)])],
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            thinking=self.THINKING_EFFORT or None,
        )
        response = await self._call_llm(request, turn=0)

        html_code = (response.text or "").strip()
        html_code = _strip_markdown_fences(html_code)

        if not html_code:
            err = ValueError("LLM returned empty HTML")
            self._on_agent_error(err, "pdf_generation")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="LLM returned empty HTML — no PDF produced",
            )

        try:
            pdf_bytes = await self._runner.run(html_code, self.NODE_TIMEOUT)
        except PuppeteerRunnerError as exc:
            logger.warning("PdfGeneratorAgent: Puppeteer error: %s", exc)
            self._on_agent_error(exc, "pdf_generation")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"PDF rendering failed: {exc}",
            )

        base_filename, display_name = _extract_filename_from_html(html_code)

        token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "PdfGeneratorAgent: pdf=%d bytes html=%d bytes tokens=%d",
            len(pdf_bytes), len(html_code), token_count,
        )
        self._on_agent_success(len(html_code), token_count, output_text="pdf_generated")

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
                        "content_b64": base64.b64encode(html_code.encode("utf-8")).decode("utf-8"),
                        "filename": f"{base_filename}.html",
                        "content_type": "text/html; charset=utf-8",
                        "label": f"{display_name}.html",
                        "file_upload": False,
                    },
                ),
                DeliveryItem(
                    type="document",
                    data={
                        "content_b64": base64.b64encode(pdf_bytes).decode("utf-8"),
                        "filename": f"{base_filename}.pdf",
                        "content_type": "application/pdf",
                        "label": f"{display_name}.pdf",
                        "file_upload": True,
                    },
                ),
            ],
        )

    async def _build_system_prompt(self, account_id: Optional[str]) -> str:
        return await self.prompt_builder.build_for_agent(
            account_id=account_id,
            agent_type="pdf_generator",
            user_id=self.user_id,
        )

    def _get_alternative_agents(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(html_code: str) -> str:
    """Remove accidental ```html ... ``` or ``` ... ``` wrapping from LLM output."""
    if "```" not in html_code:
        return html_code
    if html_code.startswith("```"):
        # Skip the opening fence line (```html or ```)
        first_newline = html_code.find("\n")
        html_code = html_code[first_newline + 1:] if first_newline >= 0 else html_code[3:]
    html_code = html_code.rsplit("```", 1)[0].strip()
    return html_code


def _extract_filename_from_html(html_code: str) -> tuple[str, str]:
    """
    Extract (base_filename, display_name) from the HTML <title> tag.

    base_filename: sanitized lowercase slug (alphanumerics, underscores, hyphens).
    display_name:  raw title text as-is (used for Slack label).

    Falls back to ("document", "Document") when <title> is absent or empty.
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html_code, re.IGNORECASE | re.DOTALL)
    title = match.group(1).strip() if match else ""
    if not title:
        return "document", "Document"

    display_name = title
    base_filename = "".join(
        c if c.isalnum() or c in ("_", "-") else "_" for c in title.lower()
    )
    base_filename = re.sub(r"_+", "_", base_filename).strip("_") or "document"
    return base_filename, display_name
