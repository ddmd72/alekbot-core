"""Where two round-3 fixes meet in the planner (R3 merge; USER_DRIVE_INDEXING_RFC §3.4, §7.3, §8.2, §24.8).

`unresolved` became a scope hold that keeps a file's content outcome (F2-1), and the reader's outages are charged to the
file's content version (F1-1: "a content outcome or a new version starts it again"). A file that changes while its
parent is unresolved is a new version: when the parent resolves, it must not inherit the old version's attempts or
charged outages, or one more outage would fail the new version early. The same version keeps them. Pure."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS
from src.domain.drive_index_planner import (
    change_from_record, content_due, generation_started, outage_charged, plan_observation,
)
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS

from .test_drive_index_planner_unresolved_hold import CTX, NORMAL, NOW, UNRESOLVED, pending_file, replan


@pytest.fixture(autouse=True)
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def charged(times: int):
    rec = generation_started(pending_file(), NOW)
    for _ in range(times):
        rec = outage_charged(rec, NOW + timedelta(hours=1), NOW, "reader@1")
    return rec


def test_the_same_version_keeps_its_charged_outages_through_the_hold():
    rec = charged(MAX_ATTEMPTS - 1)
    assert (rec.state, rec.model_outages) == (RecordState.PENDING, MAX_ATTEMPTS - 1)
    back = replan(replan(rec, UNRESOLVED), NORMAL)
    assert (back.state, back.model_outages) == (RecordState.PENDING, MAX_ATTEMPTS - 1)
    # one more outage of the same version fails it, as without the hold
    assert outage_charged(back, NOW + timedelta(hours=1), NOW, "reader@1").state is RecordState.FAILED


def test_a_new_version_under_the_hold_starts_its_outages_and_attempts_again():
    rec = replace(charged(MAX_ATTEMPTS - 1), attempts=MAX_ATTEMPTS - 1)
    held = replan(rec, UNRESOLVED)
    edited = replace(change_from_record(held), content_version="c2")
    held_edit = plan_observation(edited, held, UNRESOLVED, None, CTX, UNRESOLVED).upsert
    assert held_edit.state is RecordState.UNRESOLVED
    assert (held_edit.model_outages, held_edit.attempts, held_edit.defer_until) == (0, 0, None)
    back = replan(held_edit, NORMAL)
    assert (back.state, back.content_version, back.model_outages, back.attempts) == (RecordState.PENDING, "c2", 0, 0)
    assert content_due(back)
    # the new version gets its full MAX_ATTEMPTS outages before it fails
    again = outage_charged(back, NOW + timedelta(hours=1), NOW, "reader@1")
    assert (again.state, again.model_outages) == (RecordState.PENDING, 1)


def test_a_folder_under_the_hold_is_left_to_its_own_rules():
    folder = replace(UNRESOLVED, item_id="G", parent_id="F", attempts=2)
    edited = replace(change_from_record(folder), content_version="v2")
    after = plan_observation(edited, folder, UNRESOLVED, None, CTX, UNRESOLVED).upsert
    assert (after.state, after.content_version, after.attempts) == (RecordState.UNRESOLVED, "v2", folder.attempts)
