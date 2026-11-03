"""Composition helper: the drive connector's one-time link service (DRIVE_MCP_CONNECTOR_RFC §8).

`main()` calls `build_drive_link_service` inside the drive connector's build (it may construct no `src` class itself,
`tests/unit/test_main_construction_allowlist.py`): the Firestore link store, the aiohttp byte stream and the service,
whose grant check is the shared authorization service's `grant_allowed` (injected, so the service imports no other
service). The links' host is the scheme and host of `MCP_RESOURCE_URI` (E4: no new deploy key).
"""
from typing import Optional

from ..adapters.firestore_drive_link_store import FirestoreDriveLinkStore
from ..adapters.http_media_stream_adapter import HttpMediaStreamAdapter
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain.drive_link import link_base_url
from ..services.drive_link_service import DriveLinkService
from ..services.mcp_authorization_service import MCPAuthorizationService
from ..services.user_drive_service import UserDriveService


def build_drive_link_service(*, db_client, env_config: EnvironmentConfig,
                             drive_service: Optional[UserDriveService],
                             authorization_service: MCPAuthorizationService, digest_key: bytes,
                             auth_config: AuthConfig) -> DriveLinkService:
    """The service; raises when the deployment has no drive service (the drive connector is then not built)."""
    if drive_service is None:
        raise RuntimeError("the one-time drive link needs the drive service, which this deployment does not have")
    return DriveLinkService(
        FirestoreDriveLinkStore(db_client, env_config), drive_service, HttpMediaStreamAdapter(),
        grant_allowed=authorization_service.grant_allowed, digest_key=digest_key,
        base_url=link_base_url(auth_config.mcp_resource_uri))
