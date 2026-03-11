from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.domain.billing import BillingAccount


@pytest.mark.asyncio
async def test_family_plan_quota_enforcement():
    db_client = MagicMock()
    account_repo = FirestoreAccountRepository(db_client, "dev_")

    account = BillingAccount(
        account_id="family-1",
        daily_token_limit=100,
        usage=BillingAccount().usage
    )
    account.usage.daily_tokens = 100

    account_repo.get_account = AsyncMock(return_value=account)

    has_quota, reason = await account_repo.check_quota("family-1")
    assert not has_quota
    assert reason == "Daily token quota exceeded"
