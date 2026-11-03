"""
Main-service / indexer side of the sandboxed parser (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.10, §7 S3).

Public interface:
    build_sandbox_parser(url, *, transport=None) -> SandboxContentExtractor

One object serves both ports (`ContentExtractorPort` and `PdfSplitterPort`), over a `SandboxHttpClient` that signs
every call with the service's ID token. Reads no environment and has no knob: the caller (P4's wiring) passes the URL
and decides whether to use it. An empty URL is refused (ValueError, ERROR logged). Nothing calls this yet.
"""
from __future__ import annotations

from typing import Any, Optional

from ..adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider, normalise_audience
from ..adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from ..adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from ..utils.logger import logger


def build_sandbox_parser(url: str, *, transport: Optional[Any] = None) -> SandboxContentExtractor:
    # `transport`: an httpx transport for tests, typed Any: a composition `sandbox_*` module imports no outbound library.
    audience = normalise_audience(url or "")
    if not audience:
        logger.error("[SandboxClient] the sandboxed parser needs the light service URL")
        raise ValueError("the sandboxed parser needs the light service URL")
    return SandboxContentExtractor(SandboxHttpClient(audience, MetadataIdTokenProvider(audience), transport=transport))
