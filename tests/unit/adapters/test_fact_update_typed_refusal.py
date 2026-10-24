"""The typed entry refuses an update the parser's rules refuse, before anything is read or written (facts S4 · T3c
review M1), and the wrappers refuse a port without the typed path when they are built (review L1).

On the evaluating Firestore fake, through the real repository, write service and adapter."""
import copy
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.agents.consolidation_agent import _TrackingFactManagement, _TrackingFactUpdates
from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_expiry import MAX_HORIZON_DAYS, MIN_YEAR, Expiry
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_update import ParsedFactUpdate, typed_update_refusal
from src.ports.fact_management_port import FactManagementPort
from src.ports.fact_update_port import FactUpdatePort
from tests.support.fact_kit import ACC, T0, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx
from tests.support.typed_writes import build_consolidation_agent

UTC = timezone.utc
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")

REFUSED = [
    ParsedFactUpdate(state=FactState.STALE),
    ParsedFactUpdate(state=FactState.SUPERSEDED),
    ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by="other", content="works at Beta"),
    ParsedFactUpdate(superseded_by="other"),
    ParsedFactUpdate(content=""),
    ParsedFactUpdate(tags=[]),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=Expiry(ExpiryKind.HORIZON, days=0)),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=Expiry(ExpiryKind.HORIZON, days=MAX_HORIZON_DAYS + 1)),
    ParsedFactUpdate(state=FactState.CURRENT, expiry=Expiry(ExpiryKind.DATE, on=date(MIN_YEAR - 1, 1, 1))),
]
IDS = ["stale", "superseded alone", "superseded with content", "successor alone", "empty content", "empty tags",
       "zero days", "too many days", "year too early"]


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed(store):
    for doc_id in ("old", "other"):
        put_raw(store, doc_id, f"works at {doc_id}", state="stale", stale_since=T0 + timedelta(days=40),
                reported_date=T0, expiry_kind="horizon", ttl_days=30, expires_at=T0 + timedelta(days=30),
                lifecycle_events=[])


@pytest.mark.parametrize("update", REFUSED, ids=IDS)
@pytest.mark.parametrize("actor", ["review", "owner"])
async def test_a_typed_update_the_rules_refuse_fails_with_nothing_written(store, update, actor):
    seed(store)
    before = copy.deepcopy(store.docs)

    res = await store.adapter.apply_fact_update(
        "old", update, ACC, context=ctx(actor), evidence_at=datetime.now(UTC), reason=ENDED,
    )

    assert res == {"fact_id": "old", "status": "failed", "message": typed_update_refusal(update)}
    assert res["message"] is not None
    assert store.docs == before


@pytest.mark.parametrize("update", REFUSED, ids=IDS)
async def test_the_check_comes_before_the_fact_is_looked_up(store, update):
    res = await store.adapter.apply_fact_update("nope", update, ACC, context=ctx("owner"), reason=ENDED)

    assert res["status"] == "failed" and res["message"] == typed_update_refusal(update)


async def test_a_valid_typed_update_still_writes(store):
    seed(store)

    res = await store.adapter.apply_fact_update(
        "old", ParsedFactUpdate(state=FactState.ARCHIVED), ACC, context=ctx("owner"), reason=ENDED,
    )

    assert res["status"] == "updated" and store.docs["old"]["state"] == "archived"


# ------------------------------------------------------------------------------------------------ the wrappers (L1)

class _ModelOnly(FactManagementPort):
    async def search_existing_facts(self, keywords, primary_query, alternative_query="", limit=20, *, timezone):
        return []

    async def create_fact(self, content, metadata, *, context):
        return {}

    async def update_fact(self, fact_id, updates, account_id, *, context):
        return {}

    async def merge_facts(self, fact_ids, merged_content, metadata, *, context):
        return {}


def test_the_typed_tracker_refuses_a_port_without_the_typed_path_when_built():
    # Reviewer edit (S4 T6b, T3c re-review L6): the typed port is the agent's own dependency, so a port without the
    # typed path is refused where it is handed over, when the agent is built.
    with pytest.raises(TypeError, match="FactUpdatePort"):
        build_consolidation_agent(fact_updates=_ModelOnly())


def test_the_run_gets_the_typed_tracker_over_the_adapter_and_the_plain_one_otherwise(store):
    # Reviewer edit (S4 T6b, T3c re-review L6): `_tracker_for` is gone. The run's tracker is the typed one over the
    # agent's two ports (production passes the adapter as both); "otherwise" is now no tracker at all: an agent given a
    # model-only port as its typed port is refused when built. The model-only tracker class offers no typed update.
    typed = _TrackingFactUpdates(store.adapter, store.adapter)
    plain = _TrackingFactManagement(_ModelOnly())

    assert isinstance(typed, _TrackingFactUpdates) and isinstance(typed, FactUpdatePort)
    assert type(plain) is _TrackingFactManagement and not isinstance(plain, FactUpdatePort)
    with pytest.raises(TypeError, match="FactUpdatePort"):
        build_consolidation_agent(fact_updates=_ModelOnly())


def test_the_kits_recorder_refuses_a_port_without_the_typed_path_when_built(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")
    try:
        with pytest.raises(TypeError, match="FactUpdatePort"):
            kit_module.WriteRecorder(_ModelOnly(), kit)
    finally:
        kit.close()
