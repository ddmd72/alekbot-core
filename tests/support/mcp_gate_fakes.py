"""Fakes for the two repositories the MCP authorization service reads at every refresh: users and the whitelist.

Only the reads the service calls are implemented (`get_user`, `get_whitelist`); invented addresses only.
"""

from typing import Dict, Optional

from src.domain.user import UserProfile
from src.domain.whitelist import WhitelistEntry

ALLOWED_EMAIL = "alex@example.com"
REMOVED_EMAIL = "tom@acme.example"


class FakeUserRepo:
    def __init__(self, users: Optional[Dict[str, Optional[str]]] = None) -> None:
        """`users` maps user_id to its email (None: a user without an email)."""
        self.users = {uid: UserProfile(user_id=uid, email=email) for uid, email in (users or {}).items()}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self.users.get(user_id)


class FakeWhitelistRepo:
    def __init__(self, emails=(ALLOWED_EMAIL,), domains=()) -> None:
        self.entry = WhitelistEntry(allowed_emails=set(emails), allowed_domains=set(domains))
        self.error: Optional[Exception] = None

    async def get_whitelist(self) -> WhitelistEntry:
        if self.error is not None:
            raise self.error
        return self.entry


def allowing_repos(*user_ids: str):
    """(user_repo, whitelist_repo) where every given user has the whitelisted email."""
    return FakeUserRepo({uid: ALLOWED_EMAIL for uid in user_ids}), FakeWhitelistRepo()
