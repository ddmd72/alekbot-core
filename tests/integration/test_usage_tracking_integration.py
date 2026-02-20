import pytest
from unittest.mock import AsyncMock, MagicMock

from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.user import UserProfile
from src.domain.billing import BillingAccount


@pytest.mark.asyncio
async def test_increment_usage_updates_user_and_account():
    db_client = MagicMock()
    env_config = MagicMock()
    env_config.firestore_collection_prefix = "dev_"

    account_repo = FirestoreAccountRepository(db_client, "dev_")
    account_repo.get_account = AsyncMock(return_value=BillingAccount(account_id="account-1"))
    account_repo.increment_account_usage = AsyncMock()

    user_repo = FirestoreUserRepository(db_client, env_config, account_repo)
    user_repo.get_user = AsyncMock(return_value=UserProfile(user_id="user-1", account_id="account-1"))

    doc_ref = MagicMock()
    doc_ref.update = AsyncMock()
    user_repo.users_col.document = MagicMock(return_value=doc_ref)

    await user_repo.increment_usage("user-1", tokens=100, cost=0.05)

    account_repo.increment_account_usage.assert_awaited_once_with(
        account_id="account-1",
        tokens=100,
        cost=0.05
    )
    assert doc_ref.update.called
