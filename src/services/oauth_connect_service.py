"""OAuthConnectService — the server-side connect flow (MULTI_DRIVE_RFC §4.2, §4.3, §5.2).

`start` stores a single-use flow (keyed by a digest of the OAuth state, the PKCE verifier sealed) and returns the
consent URL; `complete` consumes it, exchanges the code, checks the grant and saves or updates the credential. The
provider-side revoke rule is injected as `remove_credential` (no service imports another service, REQ-ARCH-22).
"""
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Mapping, Optional

from src.domain.connect_outcome import ConnectOutcome, ConnectStatus
from src.domain.connector_spec import ConnectorSpec
from src.domain.exceptions import ConnectActivationFailed, OAuthScopeNotGrantedError
from src.domain.flow_intent import FlowIntent
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.oauth_flow import OAuthFlow
from src.domain.oauth_grant import GrantResult
from src.domain.secret_digest import keyed_digest
from src.ports.cipher_port import CipherPort
from src.ports.ephemeral_store import EphemeralStore
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.oauth_provider_port import OAuthProviderPort
from src.utils.logger import logger

_FLOW_TTL_S = 600
_PURPOSE = "oauth_flow"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OAuthConnectService:
    def __init__(self, credentials: OAuthCredentialsPort, specs: Mapping[str, ConnectorSpec],
                 edges: Mapping[str, OAuthProviderPort], flows: EphemeralStore, cipher: CipherPort,
                 digest_key: bytes, remove_credential: Callable[[str, str], Awaitable[bool]],
                 activations: Mapping[Integration, Callable[[OAuthCredential], Awaitable[None]]],
                 redirect_uri: str, clock: Callable[[], datetime] = _utcnow):
        self._credentials = credentials
        self._specs = specs
        self._edges = edges
        self._flows = flows
        self._cipher = cipher
        self._digest_key = digest_key
        self._remove_credential = remove_credential
        self._activations = activations
        self._redirect_uri = redirect_uri
        self._clock = clock

    # ------------------------------------------------------------------ start

    async def start(self, user_id: str, kind: str, *, credential_id: Optional[str] = None,
                    return_path: str = "/cabinet") -> str:
        """Store a flow and return the authorization URL. ValueError: unknown kind, or a reconnect of a credential
        the user does not hold for this kind."""
        spec = self._specs.get(kind)
        edge = self._edges.get(spec.realm) if spec else None
        if spec is None or edge is None:
            raise ValueError(f"unknown connector kind: {kind}")
        intent, target_id, expected_subject, hint_label = FlowIntent.CONNECT, "", "", ""
        if credential_id is not None:
            existing = await self._credentials.get(user_id, credential_id)
            if existing is None or existing.kind != kind:
                raise ValueError("no such credential to reconnect")
            intent, target_id = FlowIntent.RECONNECT, credential_id
            expected_subject, hint_label = existing.account.subject, existing.account.label
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        digest = keyed_digest(self._digest_key, _PURPOSE, state)
        expires_at = self._clock() + timedelta(seconds=_FLOW_TTL_S)
        flow = OAuthFlow(user_id=user_id, kind=kind, intent=intent, target_id=target_id,
                         expected_subject=expected_subject, hint_label=hint_label, pkce_verifier=verifier,
                         return_path=return_path, expires_at=expires_at)
        sealed = await self._cipher.encrypt(verifier, aad=f"oauth_flows/{digest}")
        await self._flows.set(f"oauth_flow:{digest}", {
            "user_id": user_id, "kind": kind, "intent": intent.value, "target_id": target_id,
            "expected_subject": expected_subject, "pkce_verifier_ct": sealed,
            "return_path": return_path, "expires_at": expires_at.isoformat(),
        }, ttl_s=_FLOW_TTL_S)
        logger.info(f"OAuth flow started: user={user_id[:8]} kind={kind} intent={intent.value}")
        return edge.authorization_url(flow, spec, self._redirect_uri, state=state)

    # --------------------------------------------------------------- complete

    async def complete(self, state: str, code: str, *, session_user_id: str,
                       session_account_id: str) -> ConnectOutcome:
        digest = keyed_digest(self._digest_key, _PURPOSE, state)
        record = await self._flows.get_and_delete(f"oauth_flow:{digest}")
        if record is None:
            return self._done(None, "state", session_user_id)
        kind, return_path = record["kind"], record.get("return_path", "/cabinet")
        spec = self._specs.get(kind)
        edge = self._edges.get(spec.realm) if spec else None
        if spec is None or edge is None:
            return self._done(None, "state", session_user_id)
        if datetime.fromisoformat(record["expires_at"]) < self._clock():
            return self._done(kind, "state", session_user_id, return_path)
        if record["user_id"] != session_user_id:
            return self._done(kind, "user", session_user_id, return_path)
        try:
            verifier = await self._cipher.decrypt(record["pkce_verifier_ct"], aad=f"oauth_flows/{digest}")
        except Exception as exc:
            logger.error(f"OAuth flow verifier unreadable: kind={kind}: {type(exc).__name__}")
            return self._done(kind, "state", session_user_id, return_path)
        flow = OAuthFlow(user_id=record["user_id"], kind=kind, intent=FlowIntent(record["intent"]),
                         target_id=record["target_id"], expected_subject=record["expected_subject"],
                         hint_label="", pkce_verifier=verifier, return_path=return_path,
                         expires_at=datetime.fromisoformat(record["expires_at"]))
        try:
            grant = await edge.exchange(code, flow, spec, self._redirect_uri)
        except Exception as exc:
            logger.warning(f"OAuth exchange failed: user={session_user_id[:8]} kind={kind}: {type(exc).__name__}")
            return self._done(kind, "exchange", session_user_id, return_path)
        try:
            self._check_scopes(spec, grant)
        except OAuthScopeNotGrantedError:
            return self._done(kind, "scope", session_user_id, return_path)
        if flow.intent is FlowIntent.RECONNECT and grant.account.subject != flow.expected_subject:
            return self._done(kind, "account", session_user_id, return_path)
        saved = await self._store(flow, spec, grant, session_account_id)
        activation = self._activations.get(spec.integration)
        if activation is not None:
            try:
                await activation(saved)
            except ConnectActivationFailed as exc:
                # No credential may outlive a refused activation (a drive grant without its connection): removed under
                # the revoke rule, and the connect reads failed.
                logger.error(f"OAuth activation refused the consent: user={session_user_id[:8]} kind={kind}: {exc}")
                try:
                    await self._remove_credential(saved.user_id, saved.credential_id)
                except Exception as remove_exc:
                    logger.error(f"💥 Removing the refused credential failed: user={session_user_id[:8]} kind={kind}: "
                                 f"{type(remove_exc).__name__}", exc_info=True)
                return self._done(kind, "activation", session_user_id, return_path)
            except Exception as exc:
                logger.error(f"OAuth activation failed: user={session_user_id[:8]} kind={kind}: "
                             f"{type(exc).__name__}", exc_info=True)
        return self._done(kind, "connected", session_user_id, return_path)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _check_scopes(spec: ConnectorSpec, grant: GrantResult) -> None:
        missing = [s for s in spec.required_scopes if s not in grant.scopes]
        if missing:
            raise OAuthScopeNotGrantedError(f"missing scopes for {spec.kind}")

    async def _store(self, flow: OAuthFlow, spec: ConnectorSpec, grant: GrantResult,
                     account_id: str) -> OAuthCredential:
        user_id = flow.user_id
        keys = await self._credentials.list_keys(user_id, spec.kind)
        held = [c for c in [await self._credentials.get(user_id, k.credential_id) for k in keys] if c is not None]
        target: Optional[OAuthCredential] = None
        if flow.intent is FlowIntent.RECONNECT:
            target = next((c for c in held if c.credential_id == flow.target_id), None)
        if target is None:
            target = next((c for c in held if c.account.realm == grant.account.realm
                           and c.account.subject == grant.account.subject), None)
        if target is not None:
            updated = replace(target, access_token=grant.access_token, refresh_token=grant.refresh_token,
                              token_expiry=grant.token_expiry, scopes=list(grant.scopes),
                              account=grant.account, needs_reconnect=False)
            if await self._credentials.update(updated):
                return updated
        fresh = OAuthCredential(user_id=user_id, account_id=account_id, kind=spec.kind,
                                integration=spec.integration, account=grant.account,
                                access_token=grant.access_token, refresh_token=grant.refresh_token,
                                token_expiry=grant.token_expiry, scopes=list(grant.scopes))
        saved_id = await self._credentials.save(fresh)
        saved = replace(fresh, credential_id=saved_id)
        if not spec.multiple:
            for old in keys:
                if old.credential_id != saved_id:
                    await self._remove_credential(user_id, old.credential_id)
        return saved

    def _done(self, kind: Optional[str], status: ConnectStatus, user_id: str,
              return_path: str = "/cabinet") -> ConnectOutcome:
        logger.info(f"OAuth connect: kind={kind} status={status} user={user_id[:8]}")
        return ConnectOutcome(kind=kind, status=status, return_path=return_path)
