"""The one-call-per-user marker, pinned through its caller (P11 · G1-1): `VoiceCallSetupService.claim` over the real
`FirestoreEphemeralStore` on the evaluating Firestore fake (H1, index refusal ON).

The port promises no conditional write (`EphemeralStore.set` is "not a claim"), so the claim's atomicity is the
caller's. Both tests seed an expired marker, so any fix has to read the document and the interleaving is forced on that
read (`yield_on_get`).
"""
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

import src.adapters.firestore_ephemeral_store as ephemeral_module
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.services.voice_call_setup_service import VoiceCallSetupService
from tests.support.clock_instants import UTC_MIDNIGHT, pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
TICKETS = "eval_voice_tickets"
T0 = UTC_MIDNIGHT.utc + timedelta(hours=18)
MARKER_KEY = "voice_one_call:u1"


@pytest.fixture
def world(monkeypatch):
    pin_datetime(monkeypatch, ephemeral_module, T0)
    db = EvalDb(indexes=INDEXES_FILE, groups={TICKETS: "development_voice_tickets"})
    # an expired marker of an earlier call, not yet removed by the TTL policy
    db.collection(TICKETS).docs[MARKER_KEY] = {"value": {"in_flight": True, "call_id": "c0"},
                                               "expires_at": T0 - timedelta(minutes=1)}
    setup = VoiceCallSetupService(FirestoreEphemeralStore(db, TICKETS), None, None)
    return db, setup


async def test_a_claim_while_a_live_marker_exists_is_refused(world):
    db, setup = world
    results = [await setup.claim("u1", {"call_id": "c1"}, ttl_s=300),
               await setup.claim("u1", {"call_id": "c2"}, ttl_s=300)]
    assert results == [True, False]
    assert db.collection(TICKETS).docs[MARKER_KEY]["value"]["call_id"] == "c1"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G1-1: two concurrent claims both pass the one-call marker "
                          "(VoiceCallSetupService.claim; the voice_auth route has the same check-then-set)")
async def test_two_concurrent_claims_let_exactly_one_call_through(world):
    """Flips when the claim sets the marker only if it is absent or expired, inside one transaction (a port method)."""
    db, setup = world
    db.collection(TICKETS).yield_on_get = True
    trace = []

    async def claim(call_id):
        trace.append(("start", call_id))
        try:
            return await setup.claim("u1", {"call_id": call_id}, ttl_s=300)
        finally:
            trace.append(("end", call_id))

    results = await asyncio.gather(claim("c1"), claim("c2"))
    require([event for event, _ in trace[:2]] == ["start", "start"], "both claims overlapped")
    assert sorted(results) == [False, True]
