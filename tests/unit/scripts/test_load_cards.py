"""Slice 7's loader: latest row wins, only settled cards are written onto existing mail, the vector and the money meter.
Invented mail only (email_card_fixtures); the store and the embeddings are fakes."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

from scripts.email import load_cards
from scripts.email.load_cards import latest_rows, load, under_scripts_memory
from src.domain.billing import current_ledger
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_document import card_document
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect
from tests.support.email_card_store import FakeEmailCardStore
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail

USER = "u-alex"
NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
VECTOR = [0.25] * 4


def _document(email_id: str = "m1", **overrides: Any) -> dict:
    m = mail(email_id=email_id)
    inp = build_card_input(m, BOOKING_BODY, [BOOKING_PDF], ["Booking RM-55120"], direct=True, nonce=NONCE)
    card = build_card(parse_mail_answer(answer_json(**overrides)), m, [BOOKING_PDF], inp.evidence, PEOPLE)
    return card_document(card)


def _row(email_id: str, document: Any = "auto", **extra: Any) -> dict:
    doc = _document(email_id) if document == "auto" else document
    state = doc["card_state"] if doc else "pending"
    return {"email_id": email_id, "document": doc, "vector": None, "path": "x3", "state": state,
            "reason": "" if doc else "failed", **extra}


class FakeEmbeddings:
    def __init__(self, tokens: int = 100, fail: bool = False) -> None:
        self.tokens, self.fail, self.calls = tokens, fail, 0

    async def get_embedding(self, text: str, task_type: str) -> List[float]:
        assert task_type == "RETRIEVAL_DOCUMENT"
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        ledger = current_ledger()
        if ledger is not None and self.tokens:
            ledger.add("gemini-embedding-2", prompt_tokens=self.tokens)
        return list(VECTOR)


def _store(*ids: str) -> FakeEmailCardStore:
    store = FakeEmailCardStore()
    for i in ids:
        store.seed(USER, i, subject="v1 field")
    return store


async def _load(rows, store, embedder=None, *, apply=True, **kw):
    mapping = {r["email_id"]: r for r in rows}
    return await load(mapping, store, embedder or FakeEmbeddings(), PEOPLE, USER, apply=apply, now=NOW,
                      emit=lambda _: None, **kw)


async def test_carded_card_is_written_with_vector_and_lifecycle_fields():
    store, emb = _store("m1"), FakeEmbeddings()
    report = await _load([_row("m1")], store, emb, model_label="subscription-sonnet-x3")
    doc = store.docs[(USER, "m1")]
    assert report.counts["written"] == 1 and emb.calls == 1
    assert doc["card_state"] == "carded" and doc["card_vector"] == VECTOR and doc["card_vector_pending"] is False
    assert doc["card_model"] == "subscription-sonnet-x3" and doc["carded_at"] == NOW and doc["subject"] == "v1 field"
    assert doc["people_digest"]


def test_latest_row_per_id_wins(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in (_row("m1", None), _row("m1"), _row("m2"), _row("m2", None))) + "\nnot json\n")
    rows = latest_rows(path)
    assert rows["m1"]["document"] is not None and rows["m2"]["document"] is None


async def test_pending_and_gone_rows_are_not_written():
    store = _store("m1", "m2")
    rows = [_row("m1", None), {**_row("m2", None), "state": "source_gone", "reason": "gone"}]
    report = await _load(rows, store)
    assert "card_state" not in store.docs[(USER, "m1")] and "card_state" not in store.docs[(USER, "m2")]
    assert report.counts["not_carded:pending:failed"] == 1 and report.counts["not_carded:source_gone:gone"] == 1


async def test_malformed_document_is_skipped_and_counted():
    store = _store("m1")
    report = await _load([_row("m1", {"card_state": "carded"})], store)
    assert report.counts["malformed"] == 1 and "card_state" not in store.docs[(USER, "m1")]


async def test_absent_id_is_missing_and_never_created():
    store = _store()
    report = await _load([_row("m1")], store)
    assert report.counts["missing"] == 1 and store.docs == {}


async def test_already_carded_is_skipped_and_overwrite_rewrites():
    store = _store("m1")
    await _load([_row("m1")], store)
    skipped = await _load([_row("m1")], store, model_label="other")
    assert skipped.counts["already_carded"] == 1 and store.docs[(USER, "m1")]["card_model"] == "subscription-sonnet-x3"
    rewritten = await _load([_row("m1")], store, overwrite=True, model_label="other")
    assert rewritten.counts["written"] == 1 and store.docs[(USER, "m1")]["card_model"] == "other"


async def test_empty_card_gets_no_vector_and_is_not_vector_pending():
    store, emb = _store("m1"), FakeEmbeddings()
    report = await _load([_row("m1", _document("m1", substance="none"))], store, emb)
    doc = store.docs[(USER, "m1")]
    assert report.counts["written"] == 1 and emb.calls == 0
    assert doc["card_state"] == "empty" and doc["card_vector_pending"] is False and "card_vector" not in doc


async def test_embedding_failure_stores_the_card_vector_pending():
    store = _store("m1")
    report = await _load([_row("m1")], store, FakeEmbeddings(fail=True))
    doc = store.docs[(USER, "m1")]
    assert report.counts["written"] == 1 and report.counts["vector_pending"] == 1
    assert doc["card_vector_pending"] is True and "card_vector" not in doc


async def test_row_with_a_vector_uses_it_without_a_call():
    store, emb = _store("m1"), FakeEmbeddings()
    await _load([_row("m1", vector=[0.5, 0.5])], store, emb)
    assert emb.calls == 0 and store.docs[(USER, "m1")]["card_vector"] == [0.5, 0.5]


async def test_money_cap_stops_new_embeddings_and_later_cards_are_pending():
    ids = [f"m{i}" for i in range(4)]
    store, emb = _store(*ids), FakeEmbeddings(tokens=1_000_000)
    report = await _load([_row(i) for i in ids], store, emb, max_usd=0.0001)
    # one call is in flight at a time per semaphore slot: the cap is checked before each launch
    assert report.meter.usd > 0 and report.meter.tokens > 0
    assert emb.calls < len(ids) and report.counts["vector_pending"] == len(ids) - emb.calls
    assert report.counts["written"] == len(ids)


async def test_meter_reports_tokens_and_dollars_and_fails_loudly_at_zero():
    report = await _load([_row("m1")], _store("m1"), FakeEmbeddings(tokens=2_000_000))
    assert report.meter.tokens == 2_000_000 and report.meter.usd > 0
    assert f"embed_usd={report.meter.usd:.4f}" in report.lines(True)[-1]
    meter = load_cards.Meter(1.0)
    meter.calls, meter.tokens = 3, 10
    with pytest.raises(RuntimeError, match="meter bug"):
        meter.check()


async def test_adapter_without_usage_is_estimated_from_text_length():
    report = await _load([_row("m1")], _store("m1"), FakeEmbeddings(tokens=0))
    assert report.meter.calls == 1 and report.meter.tokens >= 1 and report.meter.usd >= 0


async def test_dry_run_writes_nothing_and_calls_no_embedding():
    store, emb = _store("m1"), FakeEmbeddings()
    report = await _load([_row("m1")], store, emb, apply=False)
    assert emb.calls == 0 and "card_state" not in store.docs[(USER, "m1")]
    assert report.counts["to_write"] == 1 and report.estimate_tokens > 0 and "written" not in report.counts


async def test_second_apply_writes_nothing_new():
    store, rows = _store("m1", "m2"), [_row("m1"), _row("m2")]
    first = await _load(rows, store)
    second = await _load(rows, store)
    assert first.counts["written"] == 2 and second.counts["already_carded"] == 2 and "written" not in second.counts


async def test_limit_caps_the_cards_taken():
    store = _store("m1", "m2")
    report = await _load([_row("m1"), _row("m2")], store, limit=1)
    assert report.counts["written"] == 1


def test_input_must_be_under_scripts_memory(tmp_path):
    assert not under_scripts_memory(tmp_path / "x.jsonl")
    assert under_scripts_memory(Path(load_cards.REPO) / "scripts" / "memory" / "email_cards" / "x.jsonl")


def test_cas_expect_of_a_card_free_document_is_absent():
    assert CardExpect.absent() == CardExpect(None, None)
