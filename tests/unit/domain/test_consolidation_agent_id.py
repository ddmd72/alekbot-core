"""The consolidation agent's id and the review profile id (T12c A7): one spelling for the services, the composition, the
agent and the kit."""
from src.domain.consolidation import REVIEW_PROFILE_ID, consolidation_agent_id


def test_the_agent_id_names_the_user():
    assert consolidation_agent_id("u1") == "consolidation_agent_u1"
    assert consolidation_agent_id("u1") != consolidation_agent_id("u2")


def test_the_review_profile_is_the_consolidation_review_prompt():
    assert REVIEW_PROFILE_ID == "consolidation_review"
