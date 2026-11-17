"""Who a user is and how they get in: the account, user and whitelist repositories, IAM, invite codes, the Cabinet's
sign-in (RFC docs/10_rfcs/USER_INVITE_DEEP_LINK_RFC.md) and the phone-binding OTP client (VOICE_COMPANION_RFC §4.6)."""
from dataclasses import dataclass
from typing import Any

from twilio.rest import Client as TwilioClient

from ..adapters.firebase_auth_adapter import FirebaseAuthAdapter
from ..adapters.firestore_account_repo import FirestoreAccountRepository
from ..adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from ..adapters.firestore_user_repo import FirestoreUserRepository
from ..adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..services.auth_provider_registry import AuthProviderRegistry
from ..services.authentication_service import AuthenticationService
from ..services.iam_service import IAMService
from ..services.invite_code_service import InviteCodeService
from ..services.session_service import SessionService
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
    session_service = SessionService(secret_key=auth_config.oauth_session_secret,
                                     access_token_ttl=auth_config.access_token_ttl,
                                     refresh_token_ttl=auth_config.refresh_token_ttl)
    return Identity(account_repo=account_repo, user_repo=user_repo, whitelist_repo=whitelist_repo,
                    iam_service=iam_service, invite_service=invite_service, auth_registry=auth_registry,
                    auth_service=auth_service, session_service=session_service,
                    twilio_verify_client=_twilio_verify_client(config))


def _twilio_verify_client(config: dict) -> Any:
    """The Twilio Verify service for phone-binding OTP: optional (an MVP, dev-only feature), absent unless all three
    settings are present, like the factory's telephony adapter."""
    sid, token, service_sid = (config.get("TWILIO_ACCOUNT_SID"), config.get("TWILIO_AUTH_TOKEN"),
                               config.get("TWILIO_VERIFY_SERVICE_SID"))
    if not (sid and token and service_sid):
        return None
    return TwilioClient(sid, token).verify.v2.services(service_sid)
