"""The scan reader's daily page knob of the online card step (email index v2, slice 5): `EMAIL_READER_PAGES_PER_DAY`,
read with `os.getenv` in `email_card_setup`. Unset gives the default budget, `0` gives no budget, a non-number gives
the default and one WARNING."""
import logging
from types import SimpleNamespace


from src.adapters.firestore_email_reader_budget import FirestoreEmailReaderBudget
from src.composition.email_card_setup import attach_email_card_step
from tests.support.firestore_eval import EvalDb

KNOB = "EMAIL_READER_PAGES_PER_DAY"


def attach(monkeypatch, value):
    monkeypatch.setenv("EMAIL_CARD_ONLINE", "1")
    if value is None:
        monkeypatch.delenv(KNOB, raising=False)
    else:
        monkeypatch.setenv(KNOB, value)
    indexing = SimpleNamespace(step=None, attach_card_step=lambda step: setattr(indexing, "step", step))
    container = SimpleNamespace(context_builder=object(), assembly_service=object(), gmail_provider=object(),
                                embedding_service=object(), confirmed_people=object())
    env_config = SimpleNamespace(domain_email_facts_collection="facts", email_reader_pages_collection="pages")
    assert attach_email_card_step(indexing, container, object(), EvalDb(), env_config, object(), object()) is True
    return indexing.step._reader_budget


def test_unset_builds_the_budget_with_forty_pages(monkeypatch):
    budget = attach(monkeypatch, None)
    assert isinstance(budget, FirestoreEmailReaderBudget) and budget._cap == 40


def test_a_number_sets_the_cap(monkeypatch):
    assert attach(monkeypatch, "12")._cap == 12


def test_zero_turns_the_reader_off_with_one_info_line(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        assert attach(monkeypatch, "0") is None
    assert sum("scan reader off" in r.getMessage() for r in caplog.records) == 1


def test_a_non_number_gives_the_default_and_a_warning(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        budget = attach(monkeypatch, "abc")
    assert budget._cap == 40
    assert sum(r.levelno == logging.WARNING and KNOB in r.getMessage() for r in caplog.records) == 1
