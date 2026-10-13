"""The enrichment snapshot's format (USER_DRIVE_INDEXING_RFC §25.7, F-22): every port message round-trips through
JSON, a save is stable (sorted, the same answers make the same file), another format is refused, and the parse cache
answers a seen input without the parser."""
import json
from datetime import date, datetime, timezone

import pytest

from scripts.validation.drive_eval.enrichment_snapshot import (
    EnrichmentSnapshot, items_from_json, items_to_json, outcome_from_json, outcome_to_json, photo_from_json,
    photo_to_json, profile_from_json, profile_key, profile_to_json,
)
from scripts.validation.drive_eval.parse_cache import ParseCache
from src.domain.drive_enrichment import (
    DriveProfile, HintItem, KeyValue, PhotoDescription, PhotoObject, ProfileDate, ProfileEntity,
)
from src.domain.drive_enrichment_calls import ProfileInput, ProfileLabels, ProfileRequest
from src.domain.drive_extraction import PARSER, ExtractionOutcome, ExtractionRequest, ExtractionStatus
from src.domain.drive_index import DocumentBrief

from ..services.drive_content_fakes import FakeExtractor

PROFILE = DriveProfile(
    title="Factura", what_it_is="Invoice for a bathroom repair", language="es", doc_type="invoice",
    life_domains=("home", "finance"), dates=(ProfileDate(label="issued", iso=date(2026, 3, 11)),),
    entities=(ProfileEntity(name="Reformas Ejemplo", type="organization", printed="REFORMAS EJEMPLO S.L.",
                            forms=("Reformas Ejemplo",)),),
    key_values=(KeyValue(label="invoice_number", printed="F-2026-0311", canonical="F-2026-0311"),),
    topics=("bathroom repair",), topics_native=("reparación del baño",), topic_words=("ремонт ванної",),
    label_words=("invoice", "factura"), domain_words=("home",), validity_words=())


def test_port_messages_round_trip():
    assert profile_from_json(json.loads(json.dumps(profile_to_json(PROFILE)))) == PROFILE
    items = (HintItem(text="plumbing", type="keyword", evidence="reparación de fontanería", evidence_start=3,
                      evidence_end=27),)
    assert items_from_json(json.loads(json.dumps(items_to_json(items)))) == items
    photo = PhotoDescription(description="a square", objects=(PhotoObject(name="fountain", forms=("fuente",)),),
                             scene="night", visible_text="BAR")
    assert photo_from_json(photo_to_json(photo)) == photo and photo_from_json(None) is None
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="Кассовый чек", pages_total=1, pages_sent=1,
                                brief=DocumentBrief(doc_type="receipt", doc_date=datetime(2025, 12, 1,
                                                                                         tzinfo=timezone.utc)),
                                extracted_by="reader@1", model_calls=1, model_cost=0.0006, script_rendered=True)
    back = outcome_from_json(json.loads(json.dumps(outcome_to_json(outcome))))
    assert back.text == outcome.text and back.brief == outcome.brief and back.script_rendered
    assert back.model_cost == 0.0 and back.model_calls == 0        # a replay bills nothing


def test_the_profile_key_follows_the_input_and_the_previous_labels_not_the_item():
    req = ProfileRequest(user_id="u", account_id="a", item_id="i1", kind="pdf", name="x.pdf",
                         input=ProfileInput(head="head", tail="tail"))
    same = ProfileRequest(user_id="v", account_id="b", item_id="i2", kind="pdf", name="x.pdf",
                          input=ProfileInput(head="head", tail="tail"))
    other = ProfileRequest(user_id="u", account_id="a", item_id="i1", kind="pdf", name="x.pdf",
                           input=ProfileInput(head="head", tail="tail"),
                           previous=ProfileLabels(doc_type="invoice"))
    assert profile_key(req) == profile_key(same) != profile_key(other)


def test_save_is_stable_and_another_format_is_refused(tmp_path):
    snap = EnrichmentSnapshot(meta={"provider": "openai"}, profiles={"b": {"x": 1}, "a": {"x": 2}})
    snap.save(tmp_path / "s.json")
    again = EnrichmentSnapshot.load(tmp_path / "s.json")
    assert list(again.profiles) == ["a", "b"] and again.sha256() == snap.sha256()
    (tmp_path / "bad.json").write_text(json.dumps({"format": 99}), encoding="utf-8")
    with pytest.raises(ValueError):
        EnrichmentSnapshot.load(tmp_path / "bad.json")


async def test_the_parse_cache_answers_a_seen_input_without_the_parser(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Note\n\nsome words", encoding="utf-8")
    inner = FakeExtractor(PARSER, version=2)
    request = ExtractionRequest(path=str(path), name="note.md", kind="markdown", mime_type="text/markdown",
                                user_id="u", account_id="a")
    cache = ParseCache(inner, tmp_path / "cache")
    first = await cache.extract(request)
    second = await ParseCache(inner, tmp_path / "cache").extract(request)    # a new run, the same directory
    assert len(inner.requests) == 1 and second.text == first.text == "# Note\n\nsome words"
