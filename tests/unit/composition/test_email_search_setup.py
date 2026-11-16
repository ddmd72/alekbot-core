"""The card search is built unconditionally (email index v2 slice 10: the EMAIL_SEARCH_V2 switch is gone)."""

from src.adapters.firestore_email_card_index import FirestoreEmailCardIndex
from src.composition import email_search_setup
from src.composition.email_search_setup import build_email_card_search
from src.config.environment import EnvironmentConfig
from src.services.email_card_search_service import EmailCardSearchService
from tests.support.firestore_eval import EvalDb


def test_the_card_search_is_built_with_no_switch_in_the_environment(monkeypatch):
    monkeypatch.delenv("EMAIL_SEARCH_V2", raising=False)
    embeddings, people = object(), object()
    search = build_email_card_search(EvalDb(), EnvironmentConfig(), embeddings, people)
    assert isinstance(search, EmailCardSearchService)
    assert isinstance(search._index, FirestoreEmailCardIndex)
    assert search._embeddings is embeddings and search._people is people


def test_the_module_keeps_no_switch():
    assert not hasattr(email_search_setup, "SWITCH") and not hasattr(email_search_setup, "email_search_v2_enabled")
