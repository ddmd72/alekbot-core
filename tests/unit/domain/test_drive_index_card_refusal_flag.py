"""`card_refused` (USER_DRIVE_INDEXING_RFC §8.2): set by a terminal refusal, kept while the card text is the refused
one, lifted by any other text. It never changes the record's state."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_card import card_hash
from src.domain.drive_index import RecordState
from src.domain.drive_index_planner import card_refused, schedule

from .test_drive_index_planner import ch, observe

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def _refused(name="weird.pdf"):
    return card_refused(replace(observe(ch("a", name)).upsert, attempts=2), NOW)


def test_a_refusal_keeps_the_state_and_binds_the_refused_text():
    rec = _refused()
    assert rec.card_refused and rec.state is RecordState.METADATA_ONLY and rec.attempts == 0
    assert not rec.has_card and rec.card_hash == card_hash(rec) and rec.due_at is None


def test_re_observing_the_same_text_keeps_the_flag_and_schedules_nothing():
    stored = _refused()
    plan = observe(ch("a", "weird.pdf"), stored)
    rec = plan.upsert or stored
    assert rec.card_refused and rec.due_at is None


def test_a_new_text_lifts_the_flag_and_makes_the_card_due():
    rec = observe(ch("a", "renamed.pdf"), _refused()).upsert
    assert not rec.card_refused and rec.card_hash != card_hash(rec)
    assert schedule(rec, NOW, first_seen=False).due_at == NOW
