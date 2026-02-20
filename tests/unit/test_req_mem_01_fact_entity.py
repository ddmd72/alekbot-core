import pytest
from src.domain.entities import FactEntity, FactType

@pytest.mark.requirement("REQ-MEM-01")
def test_fact_entity_scd_type_2_defaults():
    """
    Test that FactEntity initializes with correct SCD Type 2 defaults.
    Covers: REQ-MEM-01 (Versioning)
    """
    fact = FactEntity(
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id="test-lineage",
        text="Test fact",
        type=FactType.EVENT
    )
    
    assert fact.text == "Test fact"
    assert fact.is_current is True
    assert fact.valid_to is None
    assert fact.lineage_id == "test-lineage"
    assert fact.created_at is not None
