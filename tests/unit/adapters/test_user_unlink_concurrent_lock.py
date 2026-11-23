"""`unlink_platform_identity` is a field-level update: a lock (`is_active=False`) committed between its read and its
write survives."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.user import UserProfile

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USERS = "eval_users"
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={USERS: "development_users"})
    return FirestoreUserRepository(db, SimpleNamespace(domain_users_collection=USERS, is_production=False), None)


async def test_a_lock_committed_after_the_read_survives_the_unlink(repo, monkeypatch):
    await repo.create_user(UserProfile(user_id="u1", account_id="a1", display_name="Alex Doe",
                                       email="alex@example.com", created_at=T0, updated_at=T0,
                                       platform_identities={"slack": "U1", "telegram": "777"}))
    real_get = repo.get_user

    async def get_then_lock(user_id):
        user = await real_get(user_id)
        await repo.users_col.document(user_id).update({"is_active": False})
        return user

    monkeypatch.setattr(repo, "get_user", get_then_lock)
    await repo.unlink_platform_identity("u1", "slack")
    stored = repo.db.collection(USERS).docs["u1"]
    assert stored["is_active"] is False
    assert stored["platform_identities"] == {"telegram": "777"}
