"""What the main service checks and derives before anything touches the database (SECRETS_AT_REST_RFC I6, plan D-P1;
LELIK_VOICE_MCP_RFC §5.2, O8; EXTERNAL_FILES_RFC §4.4).

Each step refuses the start on a bad or missing value, here and never inside the MCP build, whose errors main()
swallows: the stored-secrets cipher and the digest key, the voice MCP token key, the Cabinet session secret and the OAuth
callback (a placeholder secret or a localhost callback on Cloud Run), the sandbox's light-service URL (the parse adapter
and the edit-video duration probe: the main service never decodes media itself).
"""
from dataclasses import dataclass, field
from typing import Optional

from ..adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from ..adapters.sandbox_client.sandbox_media_adapter import SandboxMediaAdapter
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..ports.cipher_port import CipherPort
from . import cipher_setup, voice_mcp_setup
from .deploy_topology import DeployTopology, build_deploy_topology
from .sandbox_media_setup import build_sandbox_media
from .sandbox_parse_setup import build_sandbox_parser


@dataclass(frozen=True)
class BootChecks:
    """The values validated at boot. `voice_mcp_signing_key` None: no key, every voice token refused. Keys never in a repr."""

    cipher: CipherPort
    digest_key: bytes = field(repr=False)
    voice_mcp_signing_key: Optional[str] = field(repr=False)
    auth_config: AuthConfig
    deploy_topology: DeployTopology
    parse_adapter: SandboxContentExtractor
    video_duration: SandboxMediaAdapter


def build_boot_checks(config: dict, env_config: EnvironmentConfig) -> BootChecks:
    """Run the checks in this order and build what they validate; raises on the first refusal."""
    cipher = cipher_setup.build_cipher(config.get("KMS_CRYPTO_KEY"))
    digest_key = cipher_setup.digest_key(config.get("SECRET_DIGEST_KEY"))
    voice_mcp_key = voice_mcp_setup.signing_key(
        config.get(voice_mcp_setup.KEY_NAME), oauth_secret=voice_mcp_setup.effective_oauth_secret(config),
        tools_enabled=voice_mcp_setup.voice_mcp_tools_enabled())
    auth_config = AuthConfig(config)
    auth_config.validate()
    topology = build_deploy_topology(config, env_config)
    sandbox_url = config.get("SANDBOX_PARSE_URL") or ""
    return BootChecks(
        cipher=cipher, digest_key=digest_key, voice_mcp_signing_key=voice_mcp_key, auth_config=auth_config,
        deploy_topology=topology, parse_adapter=build_sandbox_parser(sandbox_url),
        video_duration=build_sandbox_media(sandbox_url))
