import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from src.domain.prompt_v3.profile_slot import ProfileSlot, ProfileSlotType
from src.domain.prompt_v3.slot import OwnerType
from unittest.mock import MagicMock

def test_cache_key_truncation_bug():
    """Reproduce bug where 'account-' prefix is truncated instead of UUID."""
    
    # Mock service (we only need _build_cache_key which is static-ish)
    service = PromptAssemblyService(
        token_repo=MagicMock(),
        blueprint_repo=MagicMock(),
        profile_repo=MagicMock(),
        security_port=MagicMock(),
        formatter=MagicMock(),
        bio_formatter=MagicMock()
    )
    
    # Test data
    agent_type = "smart"
    user_id = os.getenv("USER_ID", "DEMO_USER")
    account_id = f"account-{user_id}"
    
    # Generate key
    key = service._build_cache_key(agent_type, account_id, user_id)
    print(f"Cache key: {key}")
    
    # Expected behavior (Full IDs)
    expected_acc_part = account_id
    expected_usr_part = user_id
    
    if f":acc:{expected_acc_part}:" in key and f":usr:{expected_usr_part}" in key:
        print("✅ SUCCESS: Cache key contains full IDs")
    else:
        print(f"❌ FAILURE: Cache key truncated incorrectly. Got: {key}")

if __name__ == "__main__":
    test_cache_key_truncation_bug()
