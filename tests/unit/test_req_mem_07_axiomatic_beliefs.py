import pytest
from src.domain.entities import FactEntity, FactType

@pytest.mark.requirement("REQ-MEM-07")
def test_axiomatic_beliefs_distinction():
    """
    Test the distinction between Objective Facts (EVENT) and Subjective Principles (PRINCIPLE).
    Covers: REQ-MEM-07 (Axiomatic Beliefs)
    """
    # 1. Create an Objective Fact (Event)
    event_fact = FactEntity(
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id="lineage-1",
        text="User bought a car in 2020",
        type=FactType.EVENT,
        tags=["history"]
    )

    # 2. Create a Subjective Principle (Anchor)
    principle_fact = FactEntity(
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id="lineage-2",
        text="Always prioritize safety over speed",
        type=FactType.PRINCIPLE,
        tags=["anchor", "safety"]
    )

    # Assertions
    assert event_fact.type == FactType.EVENT
    assert principle_fact.type == FactType.PRINCIPLE
    assert event_fact.type != principle_fact.type
    
    # Verify they are both valid entities
    assert event_fact.id is not None
    assert principle_fact.id is not None
