"""Composition helper: the voice MCP token kind's key, its knob and its service (LELIK_VOICE_MCP_RFC §5, slice S1).

- `VOICE_MCP_SIGNING_KEY` (`load_settings()`, a Secret Manager secret): the token kind's own HS256 key, used for nothing
  else and never replaced by the OAuth session secret (O8). `signing_key` validates it; `build_application_graph` calls
  it before anything else is built, because `main()` swallows the MCP build's errors.
- `VOICE_MCP_TOOLS` (an optional knob, `os.getenv`, main service, default off): "on" puts the memory and drive MCP
  servers on a plain companion call's session on xAI (`voice_mcp_servers`, slice S3; search_memory leaves that call's
  delegation) and lets `/voice/session-config` mint the call's tokens. With it on and no key the graph refuses to
  start. Off: no server, no token is minted; tokens minted elsewhere with the key still verify while their live-call
  record exists.
- `build_voice_mcp_tokens`: the service both MCP resources verify with and the voice control plane mints and revokes
  with; None without a key (every voice token is then refused).
"""
import os
from typing import Optional, Tuple

from ..config.auth import _DEV_DEFAULT_SESSION_SECRET as DEV_DEFAULT_SESSION_SECRET
from ..config.auth import AuthConfig
from ..domain.mcp_resource import MCPResource, drive_resource_uri
from ..domain.remote_tool_server import RemoteToolServer
from ..domain.voice_mcp_servers import voice_session_servers
from ..ports.alert_sink import AlertSinkPort
from ..ports.ephemeral_store import EphemeralStore
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.voice_mcp_token_service import SIGNING_KEY_MIN_CHARS, VoiceMcpTokenService
from .mcp_setup import SCOPE_OF

KNOB = "VOICE_MCP_TOOLS"
KEY_NAME = "VOICE_MCP_SIGNING_KEY"


def voice_mcp_tools_enabled() -> bool:
    """The VOICE_MCP_TOOLS knob, read here with os.getenv (not load_settings): "on" mints at session-config; anything
    else, or unset, is off."""
    return os.getenv(KNOB, "").strip().lower() == "on"


def voice_mcp_servers(config: dict) -> Tuple[RemoteToolServer, ...]:
    """The memory and drive server descriptors LelikAgent puts on an eligible call's session config (slice S3), at
    the canonical resource URIs `AuthConfig` resolves from `config` (`MCP_RESOURCE_URI` and the drive URI derived from
    it; no new URL key). Empty with the knob off: no call then carries MCP tools and search_memory stays."""
    if not voice_mcp_tools_enabled():
        return ()
    return voice_session_servers(AuthConfig(config).mcp_resource_uri)


def effective_oauth_secret(config: dict) -> Optional[str]:
    """The OAuth session secret in effect: what `AuthConfig` resolves from `config` (the dev placeholder when the
    setting is unset), never the raw setting alone."""
    return AuthConfig(config).oauth_session_secret


def signing_key(raw: Optional[str], *, oauth_secret: Optional[str], tools_enabled: bool) -> Optional[str]:
    """The stripped key, or None when it is absent and the knob is off. `oauth_secret`: the OAuth session secret in
    effect (`effective_oauth_secret`). Raises ValueError (refuse to start) when the knob is on and the key is absent,
    when a present key is under 48 characters, equals the OAuth secret in effect, or equals the public dev placeholder
    secret (the two kinds never share a key, O8)."""
    key = (raw or "").strip()
    if not key:
        if tools_enabled:
            raise ValueError(f"{KNOB} is on but {KEY_NAME} is not set. Refusing to start.")
        return None
    if len(key) < SIGNING_KEY_MIN_CHARS:
        raise ValueError(f"{KEY_NAME} must be at least {SIGNING_KEY_MIN_CHARS} characters. Refusing to start.")
    if key == DEV_DEFAULT_SESSION_SECRET:
        raise ValueError(f"{KEY_NAME} must not be the public dev placeholder secret. Refusing to start.")
    if oauth_secret and key == oauth_secret.strip():
        raise ValueError(f"{KEY_NAME} must differ from OAUTH_SESSION_SECRET. Refusing to start.")
    return key


def build_voice_mcp_tokens(
    *,
    key: Optional[str],
    store: EphemeralStore,
    digest_key: bytes,
    auth_config: AuthConfig,
    user_repo: UserRepository,
    whitelist_repo: WhitelistRepository,
    alert_sink: Optional[AlertSinkPort],
) -> Optional[VoiceMcpTokenService]:
    """The voice token service over `key` (from `signing_key`), the facts and drive resource URIs and the voice tickets
    `store`; None when there is no key."""
    if key is None:
        return None
    return VoiceMcpTokenService(
        signing_key=key,
        store=store,
        digest_key=digest_key,
        resource_uris={
            MCPResource.FACTS: auth_config.mcp_resource_uri,
            MCPResource.DRIVE: drive_resource_uri(auth_config.mcp_resource_uri),
        },
        scope_of=SCOPE_OF,
        user_repo=user_repo,
        whitelist_repo=whitelist_repo,
        alert_sink=alert_sink,
    )
