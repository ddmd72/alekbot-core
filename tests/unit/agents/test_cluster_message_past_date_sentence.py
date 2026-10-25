"""T2b review L2/L3: what the cluster message says about a copied `date` agrees with the refusals it will meet."""
from src.agents.consolidation_agent import ConsolidationAgent


def test_the_cluster_message_says_what_a_past_date_and_todays_date_are_asked_for():
    text = " ".join(ConsolidationAgent._build_cluster_message([]).split())
    assert ("A past `date` is refused if you send it back: rewrite that fact as what happened with `{kind: none}`, "
            "or give a future date if the plan still holds; do not drop it because its date passed.") in text
    assert "A `date` of today asks for a short horizon." in text
