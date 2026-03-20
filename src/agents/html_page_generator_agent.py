"""
HtmlPageGeneratorAgent
======================

Specialist agent: receives a natural-language web page creation request, generates a
complete HTML+CSS+JS document in a single LLM call, and returns it as a "document"
DeliveryItem — a public GCS link.

Registration: internal=False — exposed to LLMs via Intent.CREATE_HTML_PAGE.
Dispatched ASYNC by AgentWorkerHandler via Cloud Tasks.

payload["query"] is the raw natural language request from the orchestrator.

Delivery:
  DeliveryItem #1: HTML → GCS public URL → link in Slack

Pipeline:
  1. System prompt — loaded from PromptBuilder (agent_type="html_page").
  2. Single LLM call — model writes complete HTML+CSS+JS as raw text response.
  3. Strip accidental markdown fences from response.
  4. Resolve Unsplash placeholders: replace source.unsplash.com URLs with real photos.
  5. Extract filename and display name from <title> tag.
  6. Return DeliveryItem.

On failure (empty HTML, prompt builder error) — AgentResponse.failure().
"""

import asyncio
import base64
import re
import time
import urllib.parse
from typing import Optional

from .base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.llm import Message, MessagePart
from ..infrastructure.agent_config import HTML_PAGE_GENERATOR
from ..ports.image_search_port import ImageResult, ImageSearchPort
from ..ports.llm_port import AgentExecutionContext, LLMRequest
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

# Matches any source.unsplash.com URL that LLM writes as a placeholder.
_UNSPLASH_SOURCE_RE = re.compile(r'https://source\.unsplash\.com/[^\s"\'()>]+')
# Parses optional WxH dimensions from the path: /1920x1080/ → (1920, 1080)
_UNSPLASH_DIMS_RE = re.compile(r'/(\d+)x(\d+)/')


class HtmlPageGeneratorAgent(BaseAgent):
    """
    Specialist agent: single LLM call writes HTML+CSS+JS, delivered as a GCS public link.

    Accepts a natural-language request via payload["query"].
    Returns AgentResponse with one delivery_item on success:
      - HTML "document" (GCS public link)

    If image_search is provided, source.unsplash.com placeholder URLs written by the LLM
    are resolved to real Unsplash photos after generation (post-processing step).
    """

    TEMPERATURE = HTML_PAGE_GENERATOR.temperature
    MAX_TOKENS = HTML_PAGE_GENERATOR.max_tokens
    THINKING_EFFORT = HTML_PAGE_GENERATOR.thinking_effort

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        user_id: Optional[str] = None,
        image_search: Optional[ImageSearchPort] = None,
    ) -> None:
        super().__init__(config)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self.user_id = user_id
        self._image_search = image_search

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
        self._on_agent_start("generate_html_page")
        start_time = time.time()

        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                account_id=account_id,
                agent_type="html_page",
                user_id=self.user_id,
            )
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
            self._on_agent_error(err, "html_page_generation")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="LLM returned empty HTML — no page produced",
            )

        if self._image_search:
            html_code = await _resolve_unsplash_placeholders(html_code, self._image_search)

        base_filename, display_name = _extract_filename_from_html(html_code)

        token_count = response.usage_metadata.total_tokens if response.usage_metadata else 0
        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "HtmlPageGeneratorAgent: html=%d bytes tokens=%d duration=%dms",
            len(html_code), token_count, duration_ms,
        )
        self._on_agent_success(len(html_code), token_count)

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="html_page_generated",
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
            ],
        )

    def _get_alternative_agents(self) -> list[str]:
        return []


# ---------------------------------------------------------------------------
# Unsplash placeholder resolution
# ---------------------------------------------------------------------------

async def _resolve_unsplash_placeholders(html: str, image_search: ImageSearchPort) -> str:
    """
    Find source.unsplash.com placeholder URLs written by the LLM, fetch real photos
    from Unsplash API, and replace them in-place.

    Attribution credits are appended before </body>.
    If the API is unavailable, placeholders are left unchanged (graceful degradation).
    """
    placeholder_urls = list(dict.fromkeys(_UNSPLASH_SOURCE_RE.findall(html)))
    if not placeholder_urls:
        return html

    # Extract keywords and dimensions from each placeholder URL.
    # source.unsplash.com/1920x1080/?mountain,sunset → keywords="mountain sunset", w=1920, h=1080
    queries: dict[str, str] = {}
    dims: dict[str, tuple[int, int] | None] = {}
    for url in placeholder_urls:
        parsed = urllib.parse.urlparse(url)
        queries[url] = parsed.query.replace(",", " ").strip() or "photo"
        m = _UNSPLASH_DIMS_RE.search(parsed.path)
        dims[url] = (int(m.group(1)), int(m.group(2))) if m else None

    # Fetch all images in parallel (one result per placeholder).
    fetch_results = await asyncio.gather(
        *[image_search.search(kw, count=1) for kw in queries.values()],
        return_exceptions=True,
    )

    # Build placeholder → ImageResult map (skip failures).
    url_map: dict[str, ImageResult] = {}
    for placeholder_url, result in zip(placeholder_urls, fetch_results):
        if isinstance(result, Exception) or not result:
            continue
        url_map[placeholder_url] = result[0]

    if not url_map:
        return html

    # Replace placeholder URLs with real ones (sized via raw_url when dimensions available).
    for placeholder_url, image in url_map.items():
        d = dims.get(placeholder_url)
        if d and image.raw_url:
            w, h = d
            real_url = f"{image.raw_url}&w={w}&h={h}&fit=crop&auto=format&q=80"
        else:
            real_url = image.url
        html = html.replace(placeholder_url, real_url)

    # Inject attribution credits before </body>.
    credit_links = [
        f'<a href="{img.photographer_url}" target="_blank" rel="noopener noreferrer">'
        f"Photo by {img.photographer} on Unsplash</a>"
        for img in url_map.values()
    ]
    credits_html = (
        '<div style="font-size:11px;color:#999;text-align:center;padding:8px 16px;">'
        + " &nbsp;·&nbsp; ".join(credit_links)
        + "</div>"
    )
    html = html.replace("</body>", credits_html + "\n</body>", 1)

    logger.info(
        "HtmlPageGeneratorAgent: resolved %d Unsplash placeholders", len(url_map)
    )
    return html


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

    Falls back to ("page", "Page") when <title> is absent or empty.
    """
    match = re.search(r"<title[^>]*>(.*?)</title>", html_code, re.IGNORECASE | re.DOTALL)
    title = match.group(1).strip() if match else ""
    if not title:
        return "page", "Page"

    display_name = title
    base_filename = "".join(
        c if c.isalnum() or c in ("_", "-") else "_" for c in title.lower()
    )
    base_filename = re.sub(r"_+", "_", base_filename).strip("_") or "page"
    return base_filename, display_name
