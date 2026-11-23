"""Who a user is and how they get in: the account, user and whitelist repositories, IAM, invite codes, the Cabinet's
sign-in (RFC docs/10_rfcs/USER_INVITE_DEEP_LINK_RFC.md), its device sessions and passkeys (CABINET_DEVICE_SESSIONS_RFC) and the phone-binding OTP client (VOICE_COMPANION_RFC §4.6)."""
from dataclasses import dataclass
from typing import Any, Tuple
from urllib.parse import urlparse

from twilio.rest import Client as TwilioClient

from ..adapters.firebase_auth_adapter import FirebaseAuthAdapter
from ..adapters.firestore_device_session_repo import FirestoreDeviceSessionRepository
from ..adapters.firestore_account_repo import FirestoreAccountRepository
from ..adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from ..adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from ..adapters.firestore_user_repo import FirestoreUserRepository
from ..adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from ..adapters.webauthn_signer_verifier import WebAuthnSignerVerifier
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..services.auth_provider_registry import AuthProviderRegistry
from ..services.authentication_service import AuthenticationService
from ..services.device_session_service import DeviceSessionService
from ..services.iam_service import IAMService
from ..services.invite_code_service import InviteCodeService
from ..services.passkey_service import PasskeyService
from ..services.session_service import SessionService
from ..services.user_notification_service import UserNotificationService
from ..utils.logger import logger


@dataclass(frozen=True)
class Identity:
    """The identity stores and services. `twilio_verify_client` None: phone binding is not configured (the Cabinet's
    phone-binding routes answer 501)."""

    account_repo: FirestoreAccountRepository
    user_repo: FirestoreUserRepository
    whitelist_repo: FirestoreWhitelistRepository
    iam_service: IAMService
    invite_service: InviteCodeService
    auth_registry: AuthProviderRegistry
    auth_service: AuthenticationService
    session_service: SessionService
    twilio_verify_client: Any


def build_identity(config: dict, db_client: Any, env_config: EnvironmentConfig, *, auth_config: AuthConfig,
                   digest_key: bytes) -> Identity:
    """The repositories over `db_client`, the services over them, the Cabinet's sign-in over `auth_config`."""
    logger.info("👤 Initializing identity: accounts, users, whitelist, IAM, invite codes...")
    account_repo = FirestoreAccountRepository(db_client=db_client, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)
    invite_repo = FirestoreInviteCodeRepository(db_client, env_config, digest_key=digest_key)
    whitelist_repo = FirestoreWhitelistRepository(db_client, env_config)
    iam_service = IAMService(user_repo=user_repo, account_repo=account_repo, whitelist_repo=whitelist_repo)
    invite_service = InviteCodeService(invite_repo, user_repo, account_repo, whitelist_repo)

    logger.info("🔐 Initializing OAuth + Cabinet Services...")
    firebase_adapter = FirebaseAuthAdapter(
        project_id=auth_config.firebase_project_id,
        web_api_key=auth_config.firebase_web_api_key,
        service_account_path=auth_config.firebase_service_account,
        oauth_client_id=auth_config.google_oauth_client_id,
        oauth_client_secret=auth_config.google_oauth_client_secret,
    )
    auth_registry = AuthProviderRegistry(providers={"firebase": firebase_adapter})
    auth_service = AuthenticationService(auth_registry=auth_registry, user_repo=user_repo, account_repo=account_repo,
                                         whitelist_repo=whitelist_repo)
    session_service = SessionService(secret_key=auth_config.oauth_session_secret)
    return Identity(account_repo=account_repo, user_repo=user_repo, whitelist_repo=whitelist_repo,
                    iam_service=iam_service, invite_service=invite_service, auth_registry=auth_registry,
                    auth_service=auth_service, session_service=session_service,
                    twilio_verify_client=_twilio_verify_client(config))


# The relying party's name, shown by the browser's passkey sheet next to the account name.
PASSKEY_RP_NAME = "Alek"


@dataclass(frozen=True)
class DeviceAuth:
    """The Cabinet's device sessions and passkeys, over one store (its `revoke_all` covers both)."""

    sessions: DeviceSessionService
    passkeys: PasskeyService


def passkey_relying_party(oauth_redirect_uri: str) -> Tuple[str, str]:
    """The passkey RP ID and origin from the Google sign-in callback (CABINET_DEVICE_SESSIONS_RFC §7.3, F7): its host,
    and its scheme with host and port. For `https://cabinet.example.com/auth/callback`: (`cabinet.example.com`,
    `https://cabinet.example.com`). That URI is already deploy-correct and localhost-guarded (`AuthConfig.validate`)."""
    parsed = urlparse(oauth_redirect_uri)
    if not (parsed.scheme and parsed.hostname):
        raise ValueError(f"OAUTH_REDIRECT_URI has no scheme or host: {oauth_redirect_uri!r}")
    port = f":{parsed.port}" if parsed.port else ""
    return parsed.hostname, f"{parsed.scheme}://{parsed.hostname}{port}"


def build_device_auth(db_client: Any, env_config: EnvironmentConfig, *, session_service: SessionService,
                      notification_service: UserNotificationService, oauth_redirect_uri: str) -> DeviceAuth:
    """The Cabinet's revocable device sessions (CABINET_DEVICE_SESSIONS_RFC §7.1) and passkeys (§7.3): one store over
    `db_client`; the session service signs with `session_service`; the passkey service verifies with the WebAuthn
    relying party of `oauth_redirect_uri` and keeps its challenges in their own ephemeral collection; both announce
    through `notification_service`."""
    repo = FirestoreDeviceSessionRepository(db_client, env_config.device_sessions_collection,
                                            env_config.passkeys_collection)
    rp_id, origin = passkey_relying_party(oauth_redirect_uri)
    verifier = WebAuthnSignerVerifier(rp_id=rp_id, rp_name=PASSKEY_RP_NAME, origin=origin)
    challenges = FirestoreEphemeralStore(db_client, env_config.passkey_challenges_collection)
    return DeviceAuth(sessions=DeviceSessionService(repo, session_service, notification_service),
                      passkeys=PasskeyService(repo, verifier, challenges, notification_service))


def _twilio_verify_client(config: dict) -> Any:
    """The Twilio Verify service for phone-binding OTP: optional (an MVP, dev-only feature), absent unless all three
    settings are present, like the factory's telephony adapter."""
    sid, token, service_sid = (config.get("TWILIO_ACCOUNT_SID"), config.get("TWILIO_AUTH_TOKEN"),
                               config.get("TWILIO_VERIFY_SERVICE_SID"))
    if not (sid and token and service_sid):
        return None
    return TwilioClient(sid, token).verify.v2.services(service_sid)
