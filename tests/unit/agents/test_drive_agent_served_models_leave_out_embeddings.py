"""The [DriveAgent] line and the turn-limit alert name the LLMs that ran; embeddings billed on the same ledger are left out."""

from src.agents.drive_agent import DriveAgent
from src.domain.billing import EMBEDDING_MODELS, ledger_scope
from src.adapters.gemini_embedding_adapter import _MODEL as ADAPTER_EMBEDDING_MODEL


def _agent() -> DriveAgent:
    agent = object.__new__(DriveAgent)   # _served_models reads only the open ledger and the configured model name
    agent.model_name = "configured-model"   # type: ignore[misc]
    return agent


def test_the_adapters_model_is_an_embedding_model():
    assert ADAPTER_EMBEDDING_MODEL in EMBEDDING_MODELS


def test_embedding_legs_do_not_appear_in_the_served_models():
    with ledger_scope("account-a") as ledger:
        ledger.add("model-a", prompt_tokens=10)
        ledger.add(ADAPTER_EMBEDDING_MODEL, prompt_tokens=5)
        ledger.add("model-b", prompt_tokens=10)
        assert _agent()._served_models() == "model-a+model-b"


def test_an_embedding_only_ledger_falls_back_to_the_configured_model():
    with ledger_scope("account-a") as ledger:
        ledger.add(ADAPTER_EMBEDDING_MODEL, prompt_tokens=5)
        assert _agent()._served_models() == "configured-model"
