"""Wiring of the integration connect flow (MULTI_DRIVE_RFC §4.3, §5.1, §5.2): the realm edges, the per-integration
activations, the revocation service and the connect service.

Each activation runs after `OAuthConnectService` saved a consent (it logs an activation that raises, and the connect
still counts). They are composition's, so no service imports another (REQ-ARCH-22), and every step inside one is best
effort, logged, as the four old callbacks did:
- MAIL: R1's `consented` (the disconnect mark and the notice flag go).
- TASKS: only for the To Do kind (compared with the To Do spec resolved once, here): `consented`, then the
  `setup_microsoft_todo` task (its read-modify-save of the config then reads the cleared flag). Google Tasks: nothing.
- DRIVE: the drive activation built by `drive_connection_setup.build_drive_activation` (create or reconnect the
  `DriveConnection`, MULTI_DRIVE_RFC §4.4, §5.2); none when no drive is wired. No R1 clear: a drive's reconnect state is
  its connection's.

Public interface:
    build_oauth_edges(config) -> List[OAuthProviderPort]
    build_connect_activations(*, reconnect, todo_connector, task_queue, drive_activation)
        -> Dict[Integration, Activation]
    build_oauth_connect_service(db_client, env_config, *, connectors, credentials, cipher, digest_key, revocation,
                                activations, redirect_uri) -> OAuthConnectService
"""
from typing import Any, Dict, List, Optional

from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..adapters.google.google_oauth_provider import GoogleOAuthProvider
from ..adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from ..config.environment import EnvironmentConfig
from ..domain.connector_spec import ConnectorSpec
from ..domain.integration import Integration
from ..domain.oauth_credential import OAuthCredential
from ..ports.cipher_port import CipherPort
from ..ports.oauth_provider_port import OAuthProviderPort
from ..ports.task_queue import TaskQueue
from ..services.credential_revocation_service import CredentialRevocationService
from ..services.integration_reconnect_service import IntegrationReconnectService
from ..services.oauth_connect_service import OAuthConnectService
from ..utils.logger import logger
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from .connector_registry import ConnectorRegistry
from .drive_connection_setup import Activation


def build_oauth_edges(config: dict) -> List[OAuthProviderPort]:
    """One edge per realm from the existing client settings: Google from the one Google OAuth client (Gmail and Google
    Tasks share it), Microsoft from the To Do app registration (the drive shares it), only when both its id and
    secret are set, as the Microsoft data adapters are."""
    edges: List[OAuthProviderPort] = [GoogleOAuthProvider(
        client_id=config.get("GOOGLE_OAUTH_CLIENT_ID") or "",
        client_secret=config.get("GOOGLE_OAUTH_CLIENT_SECRET") or "",
    )]
    if config.get("MICROSOFT_TODO_CLIENT_ID") and config.get("MICROSOFT_TODO_CLIENT_SECRET"):
        edges.append(MicrosoftOAuthProvider(
            client_id=config["MICROSOFT_TODO_CLIENT_ID"],
            client_secret=config["MICROSOFT_TODO_CLIENT_SECRET"],
        ))
    return edges


def build_connect_activations(*, reconnect: IntegrationReconnectService, todo_connector: ConnectorSpec,
                              task_queue: Optional[TaskQueue],
                              drive_activation: Optional[Activation]) -> Dict[Integration, Activation]:
    """The activation of each integration (module docstring). `drive_activation` is None when no drive is wired: a
    drive consent then only stores its credential (logged)."""

    async def consented(user_id: str, integration: Integration) -> None:
        # R1 (SECRETS_AT_REST_RFC §6.2): best effort, the credential is already stored; a failure only keeps the
        # next loss of this integration silent.
        try:
            await reconnect.consented(user_id, integration)
        except Exception as exc:
            logger.error(f"💥 Clearing the reconnect state of {integration.value} failed for user={user_id[:8]}: "
                         f"{type(exc).__name__}", exc_info=True)

    async def mail(credential: OAuthCredential) -> None:
        await consented(credential.user_id, Integration.MAIL)

    async def tasks(credential: OAuthCredential) -> None:
        if credential.kind != todo_connector.kind:
            return                       # Google Tasks: nothing runs after its consent, as before
        await consented(credential.user_id, Integration.TASKS)
        if task_queue is None:
            return
        try:     # idempotent: the primary list and the subscriptions
            await task_queue.enqueue_worker_task("setup_microsoft_todo", {"user_id": credential.user_id})
        except Exception as exc:
            logger.error(f"💥 Enqueueing the To Do setup failed for user={credential.user_id[:8]}: "
                         f"{type(exc).__name__}", exc_info=True)

    async def drive(credential: OAuthCredential) -> None:
        if drive_activation is None:
            logger.warning(f"Drive consent of user={credential.user_id[:8]} stored with no drive wired; "
                           f"no connection created")
            return
        await drive_activation(credential)

    return {Integration.MAIL: mail, Integration.TASKS: tasks, Integration.DRIVE: drive}


def build_oauth_connect_service(db_client: Any, env_config: EnvironmentConfig, *, connectors: ConnectorRegistry,
                                credentials: OAuthCredentialsPort, cipher: CipherPort, digest_key: bytes,
                                revocation: CredentialRevocationService, activations: Dict[Integration, Activation],
                                redirect_uri: str) -> OAuthConnectService:
    """The connect service over the registry's specs and edges; its flows live in their own ephemeral collection
    (`{prefix}oauth_flows`), keyed by the stage-0 digest key, the PKCE verifier sealed by the stored-secrets cipher."""
    return OAuthConnectService(
        credentials, connectors.specs(), connectors.edges(),
        FirestoreEphemeralStore(db_client, env_config.oauth_flows_collection),
        cipher, digest_key, revocation.remove, activations, redirect_uri,
    )
