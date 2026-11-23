"""A user store with alex, for the account-lock checks of the entry points (CABINET_DEVICE_SESSIONS_RFC §7.8).

`AlexStore` is the real `FirestoreUserRepository` over an `EvalDb` holding `alex@example.com` (`user-alex`, account
`acct-alex`, Slack `U-alex`, Telegram `7001`) and an active `user-other`. `lock()` writes the flag as the facts PIN
transaction does (`is_active = false`, `inactive_reason`), straight into the stored document, so every check that reads
the user fresh sees it and any cached copy would not.
"""
from types import SimpleNamespace

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.user import InactiveReason, UserProfile
from tests.support.firestore_eval import EvalDb

ALEX_EMAIL = "alex@example.com"
USERS = "users"

ALEX = UserProfile(user_id="user-alex", email=ALEX_EMAIL, display_name="Alex", account_id="acct-alex",
                   external_user_id="firebase|alex", auth_metadata={"email": ALEX_EMAIL},
                   platform_identities={"slack": "U-alex", "telegram": "7001"})
OTHER = UserProfile(user_id="user-other", email="other@example.com", display_name="Other", account_id="acct-other",
                    platform_identities={"slack": "U-other"})


class AlexStore:

    def __init__(self, db=None) -> None:
        self.db = db if db is not None else EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))

    async def seed(self) -> "AlexStore":
        for user in (ALEX, OTHER):
            await self.users.create_user(user.model_copy(deep=True))
        return self

    async def lock(self, user_id: str = "user-alex", reason: InactiveReason = InactiveReason.DURESS) -> None:
        await self.db.collection(USERS).document(user_id).update({"is_active": False, "inactive_reason": reason.value})
