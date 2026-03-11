import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.user import UserProfile


@pytest.mark.asyncio
async def test_concurrent_usage_updates():
    db_client = MagicMock()
    env_config = MagicMock()
    env_config.firestore_collection_prefix = "dev_"

    account_repo = FirestoreAccountRepository(db_client, "dev_")
    account_repo.increment_account_usage = AsyncMock()

    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)
    user_repo.get_user = AsyncMock(return_value=UserProfile(user_id="user-1", account_id="account-1"))

    async def call_increment():
        await user_repo.increment_usage("user-1", tokens=10, cost=0.01)

    await asyncio.gather(*[call_increment() for _ in range(5)])

    assert account_repo.increment_account_usage.await_count == 5
