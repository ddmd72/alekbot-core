"""The Cabinet blueprint over real services on one evaluating Firestore fake (P19), for the route-level tests of the
account, settings and integration write routes.

Real: the session service (signed JWTs), the user, account, whitelist, invite-code, indexed-email and agent-note
repositories, `InviteCodeService`. Only the collaborators the tests script are stand-ins: a Twilio-free build, so the
phone routes answer 501 here (`test_user_cabinet_app_phone_binding.py` owns them), and `task_setup` where a test needs one.
Examples come from the invented cast (usr-1/acc-1 `alex@example.com`, usr-2/acc-2 `clara@example.com`).
"""
from types import SimpleNamespace
from typing import Any, Optional

from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.services.invite_code_service import InviteCodeService
from src.services.session_service import SessionService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_store import FactStore
from tests.support.firestore_eval import EvalDb
from src.composition.connector_registry import ConnectorRegistry

SECRET = "p19-session-secret-0123456789abcdef-xyz"
ALEX = ("usr-1", "acc-1", "alex@example.com")
CLARA = ("usr-2", "acc-2", "clara@example.com")
USERS, ACCOUNTS, WHITELIST, INVITES, NOTES, MAILS, MAIL_STATE = (
    "users", "accounts", "whitelist", "invites", "notes", "mail_facts", "mail_state")


class CabinetWorld:
    def __init__(self, *, task_setup: Any = None, tasks_provider: Any = None, clock_ttl: int = 86400) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(
            domain_users_collection=USERS, domain_whitelist_collection=WHITELIST, domain_invite_codes_collection=INVITES,
            orchestrator_notes_collection=NOTES, domain_email_facts_collection=MAILS,
            email_indexing_state_collection=MAIL_STATE, is_production=False)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        self.invites = FirestoreInviteCodeRepository(self.db, env, digest_key=b"k" * 32)
        self.whitelist = FirestoreWhitelistRepository(self.db, env)
        self.invite_service = InviteCodeService(self.invites, self.users, self.accounts, self.whitelist)
        self.notes = FirestoreAgentNoteAdapter(self.db, env)
        self.mails = FirestoreIndexedEmailRepository(self.db, env)
        self.facts = FactStore()
        self.sessions = SessionService(secret_key=SECRET, access_token_ttl=clock_ttl, refresh_token_ttl=clock_ttl)
        self.app = Quart("cabinet_routes")
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=self.invite_service, session_service=self.sessions, user_repo=self.users,
            fact_repo=self.facts.repo, embedding_service=self.facts.embedder, indexed_email_repo=self.mails,
            agent_note_port=self.notes, task_setup=task_setup, tasks_provider=tasks_provider,
            connectors=ConnectorRegistry()))
        self.client = self.app.test_client()

    async def seed(self, who=ALEX, *, role: str = "owner", account: Optional[BillingAccount] = None) -> None:
        """The user and the account they belong to; `role` is the user's role in the account's IAM policy."""
        user_id, account_id, email = who
        await self.accounts.create_account(account or BillingAccount(account_id=account_id, iam_policy={user_id: role}))
        await self.users.create_user(UserProfile(user_id=user_id, account_id=account_id, email=email))

    async def allow(self, *emails: str) -> None:
        await self.db.collection(WHITELIST).document("config").set({"allowed_emails": list(emails), "allowed_domains": []})

    def token(self, who=ALEX, *, role: str = "owner") -> dict:
        user_id, account_id, email = who
        account = BillingAccount(account_id=account_id, iam_policy={user_id: role})
        jwt_ = self.sessions.create_access_token(UserProfile(user_id=user_id, account_id=account_id, email=email), account)
        return {"Authorization": f"Bearer {jwt_}"}

    def user_doc(self, user_id: str) -> dict:
        return self.db.collection(USERS).docs[user_id]
