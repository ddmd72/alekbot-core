"""
Indexer side of the sandboxed media decoder (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §7 S5; S5 design §2.6, §2.7).

Public interface:
    build_sandbox_media(url) -> SandboxMediaAdapter

The `MediaAudioPort` over the light service's media tunnel: a `SandboxTunnelClient` that signs every call with the
service's ID token (audience = the light URL, the same `SANDBOX_PARSE_URL` the parser uses), and drive reads through
`HttpRangeSource`, which keeps the drive's URL and token in the indexer. Reads no environment and has no knob (O14's
pattern: a required URL, rollback by revision). An empty URL is refused (ValueError, ERROR logged).
"""
from __future__ import annotations

from ..adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider, normalise_audience
from ..adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from ..adapters.sandbox_client.sandbox_tunnel_client import SandboxTunnelClient
from ..utils.logger import logger


def build_sandbox_media(url: str) -> SandboxMediaAdapter:
    audience = normalise_audience(url or "")
    if not audience:
        logger.error("[SandboxMedia] the sandboxed media decoder needs the light service URL")
        raise ValueError("the sandboxed media decoder needs the light service URL")
    return SandboxMediaAdapter(SandboxTunnelClient(audience, MetadataIdTokenProvider(audience)))
