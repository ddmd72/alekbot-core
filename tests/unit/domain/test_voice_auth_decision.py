from src.domain.voice_auth_decision import AuthDecision


def test_auth_decision_carries_user_and_account_only():
    decision = AuthDecision(user_id="u1", account_id="a1")
    assert decision.user_id == "u1"
    assert decision.account_id == "a1"
    assert not hasattr(decision, "level")
