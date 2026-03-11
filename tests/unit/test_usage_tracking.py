from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.user import UserProfile


@pytest.mark.asyncio
async def test_increment_usage_delegates_to_account_repo():
    """Usage tracking is delegated to account-level repo (OAuth multi-tenant refactor)."""
    db_client = MagicMock()
    env_config = MagicMock()
    env_config.firestore_collection_prefix = "dev_"
    account_repo = AsyncMock()

    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)

    user = UserProfile(user_id="user-1", account_id="account-1")
    user_repo.get_user = AsyncMock(return_value=user)

    await user_repo.increment_usage(user_id="user-1", tokens=50, cost=0.01)

    account_repo.increment_account_usage.assert_awaited_once_with(
        account_id="account-1",
        tokens=50,
        cost=0.01
    )


@pytest.mark.asyncio
async def test_increment_usage_skips_when_no_account_id():
    """Usage tracking is skipped (with warning) when user has no account_id."""
    db_client = MagicMock()
    env_config = MagicMock()
    env_config.firestore_collection_prefix = "dev_"
    account_repo = AsyncMock()

    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)

    user = UserProfile(user_id="user-2", account_id=None)
    user_repo.get_user = AsyncMock(return_value=user)

    await user_repo.increment_usage(user_id="user-2", tokens=25, cost=0.02)

    account_repo.increment_account_usage.assert_not_awaited()
