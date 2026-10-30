"""A card write never restarts the per-file attempts of pending content or triage work (USER_DRIVE_INDEXING_RFC §8.2).
Pure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain import drive_index_policy as policy
from src.domain.drive_card import card_hash
from src.domain.drive_index import DriveChange, EpochPhase, Reason
from src.domain.drive_index_planner import (
    PlanContext, card_refused, content_retry, plan_observation, with_card,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def first(name="a.pdf", **kw):
    change = DriveChange(item_id="a", name=name, parent_id="root", size_bytes=10, content_version="c1",
                         modified_at=NOW - timedelta(days=1), **kw)
    ctx = PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id="root", epoch=1, phase=EpochPhase.IDLE, now=NOW,
                      place_of=lambda lat, lon: None)
    return plan_observation(change, None, None, None, ctx).upsert


def test_with_card_keeps_content_attempts():
    retrying = content_retry(first(), Reason.EXTRACT_ERROR, NOW)
    assert with_card(retrying, card_hash(retrying), "s/8", NOW).attempts == 1


def test_with_card_keeps_triage_attempts():
    retrying = content_retry(first("IMG.jpg", mime_type="image/jpeg"), Reason.DOWNLOAD_ERROR, NOW)
    assert with_card(retrying, card_hash(retrying), "s/8", NOW).attempts == 1


def test_card_refusal_keeps_content_attempts():
    retrying = content_retry(first(), Reason.EXTRACT_ERROR, NOW)
    assert card_refused(retrying, NOW).attempts == 1


def test_without_pending_work_a_card_ends_its_own_count(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    rec = replace(first(), attempts=2)   # card refusals counted
    assert with_card(rec, card_hash(rec), "s/8", NOW).attempts == 0
    assert card_refused(rec, NOW).attempts == 0
