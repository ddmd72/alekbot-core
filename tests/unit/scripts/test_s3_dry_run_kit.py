"""Facts S3 · T12a: the model dry-run kit never writes live data and runs the real pass on an overlay.

The live store is the evaluating Firestore fake behind the real repository; the kit's repository, the real
`FactManagementAdapter` and the real agent loop run on top of it with a scripted LLM boundary. Every assertion reads
stored state (the live documents, the overlay's reads, the report), not a call.
"""
import asyncio
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import (
    S3_TOKEN_IDS,
    BudgetExceeded,
    CostMeter,
    DryRunKit,
    KitNotPrepared,
    LedgerBusy,
    NoWriteFactRepository,
    NoWriteIndexedEmails,
    PathOutsideMemory,
    RecordingCounters,
    SnapshotTokenError,
    SnapshotTokenOverlay,
    StatsSink,
    UnpricedModel,
    owner_timezone,
    reported_date_form,
)
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.config.environment import EnvironmentConfig
from src.domain.entities import FactState
from src.domain.exceptions import FactResolutionError, FactWriteConflict, InPlaceFieldRefused
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.llm import LLMResponse, ToolCall, UsageMetadata
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.domain.request_context import RequestContext
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.ports.prompt_v3.token_repository import TokenRepository
from src.ports.repository import FactRepository
from src.ports.search_enrichment_port import SearchEnrichmentPort
from src.ports.security_port import RiskLevel, SecurityPort, TrustZone, ValidationResult
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon
from tests.support.firestore_eval import EvalDb
from tests.unit.agents.test_consolidation_write_context import OMIT, calls, create, email, make_agent
from tests.unit.services.test_consolidation_owner_timezone import ACCOUNT, Accounts, Users, account, profile

OWNER = "owner-user-1"
SONNET = "claude-sonnet-5-5"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    """The kit writes only under scripts/memory; the tests point that at their own temp directory."""
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


_LIVE_WRITES = frozenset({
    "add_fact", "add_fact_if_unique", "write_superseding", "update_fact_fields", "invalidate_fact", "stale_due_fact",
    "refresh_biographical_context_cache",
})
assert _LIVE_WRITES <= FactRepository.__abstractmethods__


def _forwarded(name):
    async def read(self, *args, **kwargs):
        await asyncio.sleep(0)                       # a real store yields on every read
        return await getattr(self._real, name)(*args, **kwargs)
    return read


def _refused(name):
    async def write(self, *args, **kwargs):
        raise AssertionError(f"a live write reached the real repository: {name}")
    return write


GuardedRepo = type("GuardedRepo", (FactRepository,), {
    "__init__": lambda self, real: setattr(self, "_real", real),
    **{n: (_refused(n) if n in _LIVE_WRITES else _forwarded(n)) for n in FactRepository.__abstractmethods__},
})


def meta(**over: Any) -> Dict[str, Any]:
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["t"], "metadata": {}, "type": "event", "expiry": EXPIRY_NONE,
    }
    base.update(over)
    return base


async def seed(store: FactStore, text: str, **over: Any) -> str:
    result = await store.adapter.create_fact(text, meta(**over), context=ctx(actor="stage1"))
    assert result["status"] == "created", result
    return result["fact_id"]


def live_state(store: FactStore):
    """Every collection of the fake with its raw documents and change stamps: what 'byte-identical' means."""
    return {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in store.db.cols.items() if col.docs}


def kit_adapter(store: FactStore):
    repo = NoWriteFactRepository(GuardedRepo(store.repo))
    adapter = FactManagementAdapter(repo, store.embedder, store.write, AsyncMock(spec=SearchEnrichmentPort))
    return repo, adapter


# ----------------------------------------------------------------------------------------------- the overlay store


async def test_a_create_a_version_a_merge_and_an_invalidate_leave_the_live_store_byte_identical_and_are_recorded(store):
    first, second, third, fourth = [await seed(store, t) for t in ("alpha fact", "beta fact", "gamma fact", "delta fact")]
    before = live_state(store)
    repo, adapter = kit_adapter(store)
    context = ctx(actor="stage1", timezone="UTC")

    created = await adapter.create_fact("new fact", meta(), context=context)
    versioned = await adapter.update_fact(first, {"content": "alpha fact, corrected", "expiry": EXPIRY_NONE}, ACC, context=context)
    merged = await adapter.merge_facts([second, third], "beta and gamma", meta(), context=context)
    await repo.invalidate_fact(fourth, ACC)

    assert (created["status"], versioned["status"], merged["status"]) == ("created", "updated", "merged"), (created, versioned, merged)
    assert live_state(store) == before
    assert [w["kind"] for w in repo.writes] == ["add_fact", "write_superseding", "write_superseding", "invalidate_fact"]
    assert repo.writes[1]["replaced"] == [first] and repo.writes[1]["new_id"] == versioned["fact_id"]
    assert repo.writes[2]["replaced"] == [second, third] and repo.writes[2]["new_id"] == merged["new_fact_id"]
    # the overlay answers reads: the closed documents, the new version and the merge, the invalidated fact
    assert (await repo.get_fact_by_id(first)).state is FactState.SUPERSEDED
    assert (await repo.get_fact_by_id(first)).superseded_by == versioned["fact_id"]
    assert (await repo.get_fact_by_id(versioned["fact_id"])).text == "alpha fact, corrected"
    assert [(await repo.get_fact_by_id(i)).state for i in (second, third)] == [FactState.SUPERSEDED] * 2
    assert (await repo.get_fact_by_id(fourth)).state is FactState.INVALIDATED
    assert (await store.repo.get_fact_by_id(first)).state is FactState.CURRENT


async def test_a_part_created_in_the_pass_can_be_named_when_the_compound_it_replaces_is_retired(store):
    compound = await seed(store, "walks daily and cooks and reads")
    before = live_state(store)
    repo, adapter = kit_adapter(store)
    context = ctx(actor="cluster", timezone="UTC", model_dates=False)

    part = await adapter.create_fact("walks daily", meta(), context=context)
    retired = await adapter.update_fact(
        compound, {"state": "superseded", "superseded_by": part["fact_id"]}, ACC, context=context)
    archived = await adapter.update_fact(part["fact_id"], {"state": "archived"}, ACC, context=context)

    assert part["status"] == "created" and retired["status"] == "updated", retired
    assert archived["status"] == "updated", archived
    assert live_state(store) == before
    closed = await repo.get_fact_by_id(compound)
    assert closed.state is FactState.SUPERSEDED and closed.superseded_by == part["fact_id"]
    assert (await repo.get_fact_by_id(part["fact_id"])).state is FactState.ARCHIVED
    assert [w["kind"] for w in repo.writes] == ["add_fact", "update_fact_fields", "update_fact_fields"]


async def test_a_stale_token_conflicts_in_the_overlay_and_against_the_live_store(store):
    first, second = await seed(store, "alpha fact"), await seed(store, "beta fact")
    repo, _ = kit_adapter(store)
    stored = await repo.resolve_current(first, ACC, CONSOLIDATION_ACCEPTS)

    await repo.update_fact_fields(stored, {"last_updated": datetime(2026, 10, 1, tzinfo=timezone.utc)})
    with pytest.raises(FactWriteConflict):
        await repo.update_fact_fields(stored, {"last_updated": datetime(2026, 10, 2, tzinfo=timezone.utc)})

    stale = await repo.resolve_current(second, ACC, CONSOLIDATION_ACCEPTS)
    await store.db.collection(FACTS).document(second).update({"tags": ["changed live"]})
    with pytest.raises(FactWriteConflict):
        await repo.update_fact_fields(stale, {"last_updated": datetime(2026, 10, 3, tzinfo=timezone.utc)})
    with pytest.raises(FactWriteConflict):
        await repo.write_superseding((await repo.get_fact_by_id(first)).model_copy(update={"id": "v2"}), [stale],
                                     datetime(2026, 10, 3, tzinfo=timezone.utc), actor="x")
    assert (await repo.get_fact_by_id(second)).state is FactState.CURRENT
    assert [w["kind"] for w in repo.writes] == ["update_fact_fields"]


async def test_the_overlay_refuses_a_field_outside_the_in_place_set_and_a_version_without_vectors(store):
    first = await seed(store, "alpha fact")
    repo, _ = kit_adapter(store)
    stored = await repo.resolve_current(first, ACC, CONSOLIDATION_ACCEPTS)

    with pytest.raises(InPlaceFieldRefused):
        await repo.update_fact_fields(stored, {"text": "rewritten"})
    with pytest.raises(ValueError):
        await repo.write_superseding(stored.fact.model_copy(update={"id": "v2", "vector": None}), [stored],
                                     datetime(2026, 10, 3, tzinfo=timezone.utc), actor="x")
    with pytest.raises(ValueError):
        await repo.write_superseding(stored.fact, [], datetime(2026, 10, 3, tzinfo=timezone.utc), actor="x")
    assert repo.writes == []


async def test_invalidate_refuses_a_foreign_account_a_missing_id_and_a_page_older_than_the_text(store):
    first = await seed(store, "alpha fact")
    before = live_state(store)
    repo, adapter = kit_adapter(store)
    versioned = await adapter.update_fact(first, {"content": "alpha fact, newer", "expiry": EXPIRY_NONE}, ACC,
                                          context=ctx(actor="stage1", timezone="UTC"))

    with pytest.raises(PermissionError):
        await repo.invalidate_fact(first, "acc-other")
    with pytest.raises(ValueError):
        await repo.invalidate_fact("missing", ACC)
    with pytest.raises(FactResolutionError):                # the page showed the old text; a newer one is never deleted unseen
        await repo.invalidate_fact(first, ACC)

    assert (await repo.get_fact_by_id(versioned["fact_id"])).state is FactState.CURRENT
    assert live_state(store) == before
    assert [w["kind"] for w in repo.writes] == ["write_superseding"]


async def test_stale_due_fact_and_add_fact_if_unique_write_the_overlay_only(store):
    due_id = put_raw(store, "due-1", "walks daily", expires_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                     expiry_kind="horizon", ttl_days=30)
    before = live_state(store)
    repo, _ = kit_adapter(store)
    (due,) = await repo.find_due_facts(datetime(2026, 10, 1, tzinfo=timezone.utc))
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    await repo.stale_due_fact(due, now)
    stored, new_id = await repo.add_fact_if_unique((await repo.get_fact_by_id(due_id)).model_copy(update={"id": "extra"}))

    assert live_state(store) == before and (stored, new_id) == (True, "extra")
    fact = await repo.get_fact_by_id(due_id)
    assert fact.state is FactState.STALE and fact.stale_since == now
    assert [w["kind"] for w in repo.writes] == ["stale_due_fact", "add_fact_if_unique"]
    with pytest.raises(FactWriteConflict):
        await repo.stale_due_fact(due, now)


async def test_the_current_lists_show_the_passes_own_writes_so_the_directive_cap_backstop_sees_the_trimmed_rulebook(store):
    rule = lambda text, **o: seed(store, text, domain="agent_directive", **o)       # noqa: E731
    kept, dropped = await rule("always answer in English", context_priority="high"), await rule("never use tables")
    repo, adapter = kit_adapter(store)
    context = ctx(actor="stage2b", timezone="UTC", model_dates=False)

    await adapter.update_fact(dropped, {"state": "invalidated"}, ACC, context=context)
    added = await adapter.create_fact("cite sources", meta(domain="agent_directive", context_priority="critical"), context=context)

    ordered = await repo.get_active_facts_ordered(ACC, domain="agent_directive")
    assert [f.id for f in ordered] == [added["fact_id"], kept]
    assert [f.id for f in await repo.get_active_facts_ordered(ACC, domain="agent_directive", limit=1)] == [added["fact_id"]]
    assert {f.id for f in await repo.get_active_facts(ACC)} == {kept, added["fact_id"]}
    assert (await store.repo.get_active_facts_ordered(ACC, domain="agent_directive")) != ordered      # the live list is untouched


async def test_the_real_adapter_on_the_kit_still_refuses_a_missing_expiry(store):
    before = live_state(store)
    repo, adapter = kit_adapter(store)
    no_expiry = meta()
    del no_expiry["expiry"]

    result = await adapter.create_fact("walks daily", no_expiry, context=ctx(actor="stage1", timezone="UTC"))

    assert result["status"] == "failed" and result["refused"] == "expiry"
    assert repo.writes == [] and live_state(store) == before


# ----------------------------------------------------------------------------------------------- counters, emails


async def test_the_recording_counters_hold_what_the_production_store_would_validate_and_claim_a_day_once():
    counters = RecordingCounters()
    day = datetime(2026, 10, 25, tzinfo=timezone.utc).date()

    await counters.add("acc-1", day, {"expiry_tool_errors": 2})
    await counters.add("acc-1", day, {"expiry_tool_errors": 0})
    with pytest.raises(ValueError):
        await counters.add("acc-1", day, {"staled": 1})
    with pytest.raises(ValueError):
        await counters.add("", day, {"expiry_tool_errors": 1})

    assert counters.expiry_tool_errors == 2 and len(counters.adds) == 1
    assert [(r.account_id, r.expiry_tool_errors) for r in await counters.for_day(day)] == [("acc-1", 2)]
    assert [await counters.claim_line(day), await counters.claim_line(day)] == [True, False]


async def test_mark_consolidated_writes_nothing_and_the_live_emails_are_still_unconsolidated():
    db = EvalDb()
    env = SimpleNamespace(domain_email_facts_collection="eval_email_facts", email_indexing_state_collection="eval_email_state")
    live = FirestoreIndexedEmailRepository(db, env)
    await live.save_batch([email("e1", datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 13, 1))])
    before = copy.deepcopy(db.collection("eval_email_facts").docs)
    emails = NoWriteIndexedEmails(live)

    batch = await emails.get_unconsolidated_batch(USER)
    await emails.mark_consolidated(USER, [e.email_id for e in batch], datetime(2026, 10, 25, tzinfo=timezone.utc))
    await emails.save_batch(batch)

    assert [e.email_id for e in batch] == ["e1"] and batch[0].email_date.tzinfo is None
    assert db.collection("eval_email_facts").docs == before
    assert [e.email_id for e in await live.get_unconsolidated_batch(USER)] == ["e1"]
    assert [w["kind"] for w in emails.writes] == ["mark_consolidated", "save_batch"]
    assert emails.writes[0]["email_ids"] == ["e1"]


# ----------------------------------------------------------------------------------------------- owner zone


async def test_the_owner_zone_is_resolved_like_the_service_and_lands_in_the_context_and_the_write_contexts(tmp_path):
    accounts, users = Accounts(account(**{OWNER: "owner"})), Users(profile(OWNER, tz="Asia/Tokyo"))
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")

    assert await kit.resolve_timezone(Accounts(), users, ACCOUNT) == "UTC"          # unknown account
    assert await kit.resolve_timezone(accounts, users, ACCOUNT) == "Asia/Tokyo"
    context = kit.message_context(USER, ACC, session_id="s")
    assert context["owner_timezone"] == "Asia/Tokyo" and context["session_id"] == "s" and context["user_id"] == USER
    assert kit.write_context("stage2a").timezone == "Asia/Tokyo" and kit.write_context("stage2a").model_dates is False
    assert kit.write_context("email").model_dates is True

    assert await kit.resolve_timezone(accounts, users, ACCOUNT, override="Europe/Paris") == "Europe/Paris"
    assert kit.message_context(USER, ACC)["owner_timezone"] == "Europe/Paris"


async def test_an_unresolvable_owner_gives_utc_and_a_bad_override_is_refused():
    assert await owner_timezone(Accounts(), Users(), ACCOUNT) == "UTC"
    assert await owner_timezone(Accounts(account()), Users(), ACCOUNT) == "UTC"
    assert await owner_timezone(Accounts(raises=RuntimeError("down")), Users(), ACCOUNT) == "UTC"
    with pytest.raises(ValueError):
        await owner_timezone(Accounts(), Users(), ACCOUNT, override="Mars/Olympus")


# ----------------------------------------------------------------------------------------------- snapshot tokens


class AllowAll(SecurityPort):
    async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
        return ValidationResult(sanitized_text=text, risk_level=RiskLevel.SAFE, risk_score=0.0, patterns_detected=[],
                                action_taken="passed", metadata={})


class HeldTokens(TokenRepository):
    def __init__(self, *tokens: Token) -> None:
        self.tokens = {str(t.id): t for t in tokens}

    async def get(self, token_id):
        if str(token_id) not in self.tokens:
            raise KeyError(token_id)
        return self.tokens[str(token_id)]

    async def list_by_category(self, category):
        return [t for t in self.tokens.values() if t.category == category]

    async def list_by_class(self, token_class):
        return [t for t in self.tokens.values() if t.class_ == token_class]

    async def list_all(self):
        return list(self.tokens.values())

    async def save(self, token):
        self.tokens[str(token.id)] = token

    async def delete(self, token_id):
        del self.tokens[str(token_id)]

    async def exists(self, token_id):
        return str(token_id) in self.tokens


def held(token_id: str, body: str = "from firestore") -> Token:
    return Token(id=TokenId(token_id), category=TokenCategory("policy_set"), class_=TokenClass("policies"),
                 content=body, metadata={})


def snapshot_body(token_id: str) -> str:
    text = (Path(kit_module.SNAPSHOT_DIR) / "tokens" / "system" / f"{token_id}.groovy").read_text(encoding="utf-8")
    return text.split("\n---\n", 1)[1]


async def test_the_overlay_serves_the_snapshot_body_for_the_s3_tokens_and_firestore_for_the_rest_and_refuses_save():
    live = HeldTokens(held("CONSOLIDATION_TAXONOMY"), held("CONSOLIDATION_OUTPUT_SPEC"), held("OTHER"))
    overlay = SnapshotTokenOverlay(live, AllowAll())

    taxonomy = await overlay.get(TokenId("CONSOLIDATION_TAXONOMY"))
    assert taxonomy.content == snapshot_body("CONSOLIDATION_TAXONOMY") != "from firestore"
    front = kit_module._serializer().token_from_file(
        (kit_module.SNAPSHOT_DIR / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy").read_text(encoding="utf-8"))
    assert (taxonomy.category, taxonomy.class_) == (TokenCategory(front["category"]), TokenClass(front["class"]))
    assert [str((await overlay.get(TokenId(t))).id) for t in S3_TOKEN_IDS] == list(S3_TOKEN_IDS)
    assert (await overlay.get(TokenId("CONSOLIDATION_OUTPUT_SPEC"))).content == "from firestore"
    assert (await overlay.get(TokenId("OTHER"))).content == "from firestore"
    with pytest.raises(KeyError):
        await overlay.get(TokenId("MISSING"))
    listed = {str(t.id): t.content for t in await overlay.list_all()}
    assert listed["CONSOLIDATION_TAXONOMY"] == snapshot_body("CONSOLIDATION_TAXONOMY") and listed["OTHER"] == "from firestore"
    assert set(S3_TOKEN_IDS) <= set(listed)
    with pytest.raises(PermissionError):
        await overlay.save(held("X"))
    with pytest.raises(PermissionError):
        await overlay.delete(TokenId("OTHER"))
    assert set(live.tokens) == {"CONSOLIDATION_TAXONOMY", "CONSOLIDATION_OUTPUT_SPEC", "OTHER"}
    assert live.tokens["CONSOLIDATION_TAXONOMY"].content == "from firestore"


# ----------------------------------------------------------------------------------------------- the cost meter


def priced_agent(calls_made: List[int], tokens: int = 10_000, model: str = SONNET):
    """An object with a model boundary: each call answers with `tokens` prompt tokens and 1000 completion tokens."""
    async def call(request, turn=1):
        calls_made.append(turn)
        return LLMResponse(text="x", usage_metadata=UsageMetadata(prompt_tokens=tokens, completion_tokens=1000))

    return SimpleNamespace(_call_llm=call), SimpleNamespace(model_name=model)


async def test_the_meter_aborts_at_the_cap_and_keeps_the_ledger_across_runs(tmp_path):
    ledger = tmp_path / "s3_dry_run_spend.json"
    made: List[int] = []
    first, request = priced_agent(made)         # $2/M in + $10/M out: 10k + 1k tokens = $0.03 a call
    run_one = CostMeter(ledger, budget_usd=0.05)
    run_one.wrap(first)

    await first._call_llm(request)
    with pytest.raises(BudgetExceeded):
        await first._call_llm(request)           # $0.03 spent + a $0.03 call would pass $0.05
    assert made == [1]
    assert run_one.aborted and run_one.spent_usd == pytest.approx(0.03)
    run_one.close()

    second, _ = priced_agent(made)
    run_two = CostMeter(ledger, budget_usd=0.05)  # a new process reads the ledger
    run_two.wrap(second)
    with pytest.raises(BudgetExceeded):
        await second._call_llm(request)
    assert len(made) == 1 and run_two.spent_usd == pytest.approx(0.03)
    run_two.close()

    third, _ = priced_agent(made)
    run_three = CostMeter(ledger, budget_usd=1.0)
    run_three.wrap(third)
    await third._call_llm(request)

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["total_usd"] == pytest.approx(0.06) and data["calls"] == 2
    assert len(data["runs"]) == 2 and [r["calls"] for r in data["runs"]] == [1, 1]
    assert data["by_model"][SONNET]["calls"] == 2
    assert run_three.run_usd == pytest.approx(0.03) and not run_three.aborted
    run_three.close()


async def test_the_meter_stops_a_model_without_a_price_before_the_call(tmp_path):
    made: List[int] = []
    agent, request = priced_agent(made, model="made-up-model")
    meter = CostMeter(tmp_path / "ledger.json")
    meter.wrap(agent)

    with pytest.raises(UnpricedModel):
        await agent._call_llm(request)
    assert made == [] and not (tmp_path / "ledger.json").exists()


async def test_a_response_without_usage_is_counted_as_unmetered(tmp_path):
    async def call(request, turn=1):
        return LLMResponse(text="x")

    agent = SimpleNamespace(_call_llm=call)
    meter = CostMeter(tmp_path / "ledger.json")
    meter.wrap(agent)

    await agent._call_llm(SimpleNamespace(model_name=SONNET))

    assert meter.unmetered_calls == 1 and meter.spent_usd == 0.0


def test_the_kit_writes_only_under_scripts_memory_and_builds_nothing_at_import():
    for path in (kit_module.SPEND_LEDGER, kit_module.DEFAULT_REPORT):
        assert path.parent == kit_module.PROJECT_ROOT / "scripts" / "memory"
    assert kit_module.DEFAULT_BUDGET_USD == 30.0


@pytest.mark.parametrize("sent,form", [
    (None, None), ("", None), ("2026-10-22T12:00", "naive"), ("2026-10-22", "naive"), ("2026-10-22T12:00:00", "naive"),
    ("2026-10-22T12:00+02:00", "aware"), ("2026-10-22T10:00:00Z", "aware"), ("2026-10-22 Thu 12:00 CEST", "other"),
    (1_760_000_000, "other"),
])
def test_reported_date_forms(sent, form):
    assert reported_date_form(sent) == form


# ----------------------------------------------------------------------------------------------- the agent on the kit


def answered(text: str = '{"operations": []}') -> LLMResponse:
    return LLMResponse(text=text, usage_metadata=UsageMetadata(prompt_tokens=1000, completion_tokens=100))


def metered(response: LLMResponse) -> LLMResponse:
    return response.model_copy(update={"usage_metadata": UsageMetadata(prompt_tokens=1000, completion_tokens=100)})


def scripted(*responses: LLMResponse):
    seen = {"n": 0}

    def script(first: str, turns: str) -> LLMResponse:
        index = min(seen["n"], len(responses) - 1)
        seen["n"] += 1
        return responses[index]

    return script


class FakeProfiles:
    """The agent-profile store: the consolidation profile lists `listed` tokens; `overrides` maps an owner level to
    {token id: ProfileToken}."""

    def __init__(self, listed=S3_TOKEN_IDS, non_overridable=True, overrides=None):
        self.tokens = {tid: ProfileToken.from_dict(tid, {"order": 10 * i, "non_overridable": non_overridable})
                       for i, tid in enumerate(listed, 1)}
        self.overrides = overrides or {}

    async def get_agent_profile(self, agent_type):
        assert agent_type == "consolidation"
        return SimpleNamespace(blueprint_id="consolidation_agent_v1", tokens=self.tokens)

    async def get_override_tokens(self, level, owner_id):
        return self.overrides.get(level, {})


def assembly(real_tokens=None, **profile_options):
    service = SimpleNamespace(token_repo=real_tokens or HeldTokens(held("CONSOLIDATION_OUTPUT_SPEC")),
                              security_port=AllowAll(), profile_repo=FakeProfiles(**profile_options), cleared=0)
    service.invalidate_cache = lambda: setattr(service, "cleared", service.cleared + 1)
    return service


async def kit_agent(store: FactStore, tmp_path: Path, *responses: LLMResponse, email_repo=None, live_repo=None,
                    snapshot_dir=None, service=None, prepare: bool = True):
    agent = make_agent(store, scripted(*responses), email_repo=email_repo)
    agent.model_name = SONNET
    agent.prompt_builder.assembly_service = service or assembly()
    if live_repo is not None:
        agent._repo = live_repo
    live_db = EvalDb()
    agent._lifecycle_counters = FirestoreFactLifecycleCounters(live_db, EnvironmentConfig())
    agent._live_counters_db = live_db
    kit = DryRunKit(ledger_path=tmp_path / "spend.json", snapshot_dir=snapshot_dir or kit_module.SNAPSHOT_DIR)
    kit.timezone = "UTC"
    kit.install(agent)
    if prepare:
        await kit.prepare(USER, ACC)
    return agent, kit


async def run_pass(agent, label: str = "stage1", timezone_name: str = "UTC", model_dates: bool = True):
    return await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=USER, account_id=ACC, pass_label=label,
        write_context=ctx(actor=label, timezone=timezone_name, model_dates=model_dates),
    )


async def test_a_pass_with_an_expiry_error_leaves_the_live_counters_untouched_and_the_recording_fake_holds_it(store, tmp_path):
    missing = metered(calls(create("walks daily", expiry=OMIT)))
    agent, kit = await kit_agent(store, tmp_path, missing, answered())

    ops = await run_pass(agent)

    assert ops == []
    assert agent._live_counters_db.collection(EnvironmentConfig().fact_lifecycle_daily_collection).docs == {}
    assert kit.counters.expiry_tool_errors == 1
    (row,) = kit.passes
    assert (row["pass"], row["llm_calls"], row["tool_turns"], row["final_report"]) == ("stage1", 2, 1, True)
    assert (row["tool_error_turns"], row["expiry_refusals"], row["abandoned_refusals"], row["born_due"]) == (1, 1, 1, 0)
    assert any(post.startswith("🚨 Consolidation left expiry refusals") for post in kit.sink.posts)
    assert any(post.startswith("📊") for post in kit.sink.posts)
    assert store.docs == {}


async def test_the_pass_writes_into_the_overlay_only_and_the_report_has_one_row_per_write(store, tmp_path):
    seeded = await seed(store, "walks daily and reads at night")
    before = live_state(store)
    stage1 = metered(calls(
        create("flight booked", reported_date="2026-10-22T12:00", expiry=horizon(30)),
        create("no expiry sent", expiry=OMIT),
        create("hotel booked", reported_date="2026-10-22T12:00:00+02:00", expiry=horizon(30)),
        ToolCall(name="update_fact", args={"fact_id": seeded, "updates": {"state": "archived", "expiry": horizon(9)}}),
    ))
    agent, kit = await kit_agent(store, tmp_path, stage1, answered())

    await run_pass(agent)

    assert live_state(store) == before
    assert [(r["action"], r["status"]) for r in kit.rows] == [
        ("create", "created"), ("create", "failed"), ("create", "created"), ("update", "updated")]
    flight, refused, hotel, closed = kit.rows
    assert flight["decision_stored"]["expiry_kind"] == "horizon" and flight["decision_stored"]["ttl_days"] == 30
    assert flight["reported_date_form"] == "naive" and flight["reported_date_chosen"].startswith("2026-10-22T12:00")
    assert hotel["reported_date_form"] == "aware" and hotel["reported_date_chosen"].startswith("2026-10-22T10:00")
    assert refused["refused"] == "expiry" and refused["decision_stored"] is None and refused["fact_id"] is None
    assert closed["fact_id"] == seeded and closed["expiry_sent"] == horizon(9) and closed["state_stored"] == "archived"
    assert closed["notes"]                                  # the ignored expiry of a closing state is said
    assert all(r["actor"] == "stage1" and r["pass"] == "stage1" for r in kit.rows)
    assert kit.report()["store_writes"] == {"add_fact": 2, "update_fact_fields": 1}
    written = kit.write_report(tmp_path / "report.json")
    assert json.loads(written.read_text(encoding="utf-8"))["writes"] == json.loads(json.dumps(kit.rows, default=str))


async def test_a_failing_store_shows_as_a_failed_row_with_nothing_stored(store, tmp_path):
    agent, kit = await kit_agent(store, tmp_path, answered())

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    kit.repo.add_fact = broken
    result = await agent._fact_management.create_fact("walks daily", meta(), context=ctx(actor="stage1", timezone="UTC"))

    assert result["status"] == "failed"
    (row,) = kit.rows
    assert (row["action"], row["status"], row["fact_id"], row["decision_stored"]) == ("create", "failed", None, None)


async def test_the_report_counts_naive_reported_dates_per_pass_and_in_total_over_two_passes(store, tmp_path):
    compound = await seed(store, "walks daily and reads at night")
    first = metered(calls(create("flight booked", reported_date="2026-10-22T12:00", expiry=horizon(30)),
                          create("hotel booked", reported_date="2026-10-22T12:00:00+02:00", expiry=horizon(30))))
    second = metered(calls(
        create("walks daily", reported_date="2026-10-21T08:00", expiry=EXPIRY_NONE),
        ToolCall(name="update_fact", args={"fact_id": compound, "updates": {"reported_date": "2026-10-20"}}),
    ))
    agent, kit = await kit_agent(store, tmp_path, first, answered(), second, answered())

    await run_pass(agent, "stage1")
    await run_pass(agent, "stage2a", model_dates=False)

    report = kit.report()
    assert [p["pass"] for p in report["passes"]] == ["stage1", "stage2a"]
    assert report["reported_date_forms"]["per_pass"] == {"stage1": {"naive": 1, "aware": 1}, "stage2a": {"naive": 2}}
    assert report["reported_date_forms"]["total"] == {"naive": 3, "aware": 1}
    assert [(r["pass"], r["actor"], r["action"]) for r in kit.rows] == [
        ("stage1", "stage1", "create"), ("stage1", "stage1", "create"),
        ("stage2a", "stage2a", "create"), ("stage2a", "stage2a", "update")]
    assert report["spend"]["calls"] == 4 and report["spend"]["total_usd"] > 0 and not report["spend"]["aborted"]


async def test_install_puts_the_snapshot_overlay_on_the_prompt_builder_and_the_store_under_the_agent(store, tmp_path):
    service = assembly(HeldTokens(held("CONSOLIDATION_TOOLS")))
    agent = make_agent(store, scripted(answered()))
    agent.model_name = SONNET
    agent.prompt_builder.assembly_service = service
    live_repo = agent._repo
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")

    kit.install(agent)

    assert isinstance(agent._repo, NoWriteFactRepository) and agent._repo is kit.repo
    assert kit.repo._real is live_repo
    assert isinstance(agent._lifecycle_counters, RecordingCounters) and isinstance(agent._alert_sink, StatsSink)
    assert agent._stats_alerts is True and service.cleared == 1
    assert (await service.token_repo.get(TokenId("CONSOLIDATION_TOOLS"))).content == snapshot_body("CONSOLIDATION_TOOLS")
    with pytest.raises(RuntimeError):
        kit.install(agent)
    await agent._repo.refresh_biographical_context_cache(ACC)
    assert kit.repo.writes == [{"kind": "refresh_biographical_context_cache"}]


# ----------------------------------------------------------------------------------------------- M1: no live write service


async def test_a_v2_agent_is_refused_and_no_write_service_under_a_v3_agent_holds_the_live_repository(store, tmp_path):
    old = make_agent(store, scripted(answered()))
    old.prompt_version = "v2"
    old.prompt_builder.assembly_service = assembly()
    kit = DryRunKit(ledger_path=tmp_path / "old.json")

    with pytest.raises(ValueError, match="v3"):
        kit.install(old)
    assert old._repo is store.repo and not kit._installed                         # nothing was swapped

    agent, kit = await kit_agent(store, tmp_path, answered())
    assert agent._fact_write_service._repo is kit.repo
    assert agent._fact_management._inner._fact_write_service._repo is kit.repo
    assert agent._fact_management._inner._repo is kit.repo


async def test_the_write_service_under_the_agent_cannot_reach_the_live_store(store, tmp_path):
    guarded = GuardedRepo(store.repo)
    agent, kit = await kit_agent(store, tmp_path, answered(), live_repo=guarded)
    before = live_state(store)
    fact = {"content": "walks daily", "tags": ["t"], "type": "event", "domain": "biographical",
            "temporal_class": "stable", "context_priority": "medium", "metadata": {}, "expiry": EXPIRY_NONE}

    saved, skipped, ids = await agent._fact_write_service.add_facts_batch(ACC, USER, [fact])       # the v2 path's write

    assert (saved, skipped) == (1, 0) and ids == []
    assert live_state(store) == before
    assert [w["kind"] for w in kit.repo.writes] == ["add_fact_if_unique"]


# ----------------------------------------------------------------------------------------------- M2: the snapshot tokens


async def test_install_refuses_an_agent_without_an_assembly_service(store, tmp_path):
    agent = make_agent(store, scripted(answered()))
    agent.prompt_builder.assembly_service = None

    with pytest.raises(ValueError, match="assembly"):
        DryRunKit(ledger_path=tmp_path / "spend.json").install(agent)
    assert agent._repo is store.repo


async def test_a_missing_snapshot_file_fails_before_any_model_call(store, tmp_path):
    agent, kit = await kit_agent(store, tmp_path, answered(), snapshot_dir=tmp_path / "empty", prepare=False)

    with pytest.raises(SnapshotTokenError, match="CONSOLIDATION_COGNITIVE_PROCESS"):
        await kit.prepare(USER, ACC)
    with pytest.raises(KitNotPrepared):
        await run_pass(agent)

    assert agent.requests == [] and kit.meter.calls == 0


async def test_a_token_the_security_port_refuses_fails_prepare_instead_of_dropping_from_the_prompt(store, tmp_path):
    class Refusing(AllowAll):
        async def validate(self, text, context, zone=TrustZone.UNTRUSTED):
            return ValidationResult(sanitized_text=text, risk_level=RiskLevel.CRITICAL, risk_score=1.0,
                                    patterns_detected=["x"], action_taken="blocked", metadata={})

    service = assembly()
    service.security_port = Refusing()
    agent, kit = await kit_agent(store, tmp_path, answered(), service=service, prepare=False)

    with pytest.raises(SnapshotTokenError):
        await kit.prepare(USER, ACC)


async def test_a_profile_that_does_not_list_an_s3_token_is_refused(store, tmp_path):
    service = assembly(listed=[t for t in S3_TOKEN_IDS if t != "CONSOLIDATION_TOOLS"])
    agent, kit = await kit_agent(store, tmp_path, answered(), service=service, prepare=False)

    with pytest.raises(SnapshotTokenError, match="CONSOLIDATION_TOOLS"):
        await kit.prepare(USER, ACC)


def override_token() -> Token:
    taxonomy = kit_module._serializer().token_from_file(
        (kit_module.SNAPSHOT_DIR / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy").read_text(encoding="utf-8"))
    return Token(id=TokenId("MY_TAXONOMY"), category=TokenCategory(taxonomy["category"]),
                 class_=TokenClass(taxonomy["class"]), content="mine", metadata={})


async def test_an_override_that_would_replace_a_snapshot_token_is_refused(store, tmp_path):
    service = assembly(HeldTokens(held("CONSOLIDATION_OUTPUT_SPEC"), override_token()), non_overridable=False,
                       overrides={OwnerType.USER: {"MY_TAXONOMY": ProfileToken.from_dict("MY_TAXONOMY", {"order": 10})}})
    agent, kit = await kit_agent(store, tmp_path, answered(), service=service, prepare=False)

    with pytest.raises(SnapshotTokenError, match="user override .*MY_TAXONOMY.*CONSOLIDATION_TAXONOMY"):
        await kit.prepare(USER, ACC)
    with pytest.raises(KitNotPrepared):
        await run_pass(agent)


async def test_an_override_the_profile_blocks_is_recorded_not_refused(store, tmp_path):
    service = assembly(HeldTokens(held("CONSOLIDATION_OUTPUT_SPEC"), override_token()), non_overridable=True,
                       overrides={OwnerType.ACCOUNT: {"MY_TAXONOMY": ProfileToken.from_dict("MY_TAXONOMY", {"order": 10})}})
    agent, kit = await kit_agent(store, tmp_path, answered(), service=service)

    assert kit.report()["overrides_blocked"] == [
        {"level": "account", "override": "MY_TAXONOMY", "replaces": "CONSOLIDATION_TAXONOMY"}]


async def test_the_report_lists_the_five_served_tokens_with_the_hash_of_their_snapshot_body(store, tmp_path):
    agent, kit = await kit_agent(store, tmp_path, answered())

    served = kit.report()["served_tokens"]

    assert [t["id"] for t in served] == list(S3_TOKEN_IDS)
    for entry in served:
        assert entry["sha256"] == hashlib.sha256(snapshot_body(entry["id"]).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------------------- M3: one write at a time


async def test_two_concurrent_writes_to_the_same_fact_one_wins_and_the_other_conflicts(store):
    first = await seed(store, "alpha fact")
    repo, _ = kit_adapter(store)
    stored = await repo.resolve_current(first, ACC, CONSOLIDATION_ACCEPTS)
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    results = await asyncio.gather(
        repo.write_superseding(stored.fact.model_copy(update={"id": "v-a", "version": 2}), [stored], now, actor="a"),
        repo.write_superseding(stored.fact.model_copy(update={"id": "v-b", "version": 2}), [stored], now, actor="b"),
        return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == ["FactWriteConflict", "NoneType"]
    winner = "v-a" if results[0] is None else "v-b"
    assert (await repo.get_fact_by_id(first)).superseded_by == winner
    assert [await repo.get_fact_by_id(v) is not None for v in ("v-a", "v-b")].count(True) == 1
    assert [w["new_id"] for w in repo.writes] == [winner]


async def test_a_content_update_and_a_merge_of_the_same_fact_in_one_turn_cannot_both_close_it(store):
    first, second = await seed(store, "alpha fact"), await seed(store, "beta fact")
    repo, adapter = kit_adapter(store)
    context = ctx(actor="stage1", timezone="UTC")

    updated, merged = await asyncio.gather(
        adapter.update_fact(first, {"content": "alpha, newer", "expiry": EXPIRY_NONE}, ACC, context=context),
        adapter.merge_facts([first, second], "alpha and beta", meta(), context=context),
    )

    closing = [r for r in (updated, merged) if r["status"] in ("updated", "merged")]
    assert len(closing) >= 1
    successors = {w["new_id"] for w in repo.writes if first in w["replaced"]}
    assert len(successors) == 1                                    # one successor of the fact, as a transaction would leave
    assert (await repo.get_fact_by_id(first)).superseded_by in successors


# ----------------------------------------------------------------------------------------------- T1, T2, L4: emails and the live store


def email_store():
    store = FactStore()
    env = SimpleNamespace(domain_email_facts_collection="eval_email_facts", email_indexing_state_collection="eval_email_state")
    return store, FirestoreIndexedEmailRepository(store.db, env)


async def run_email_pass(agent, batches: int, size: int):
    message = AgentMessage(
        task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate_email", "number_of_batches": batches, "batch_size": size},
        context={"user_id": USER, "owner_timezone": "UTC"},
    )
    with RequestContext(user_id=USER, account_id=ACC):
        return await agent.execute(message)


async def test_the_email_pass_on_the_kit_marks_nothing_live_and_reads_each_email_once(tmp_path):
    store, live_emails = email_store()
    await live_emails.save_batch([email("e1", datetime(2026, 10, 10, 9, 0), datetime(2026, 10, 13, 1)),
                                  email("e2", datetime(2026, 10, 12, 9, 0), datetime(2026, 10, 13, 2))])
    before = live_state(store)
    agent, kit = await kit_agent(
        store, tmp_path, metered(calls(create("flight booked"))), answered(), metered(calls(create("hotel booked"))),
        answered(), email_repo=live_emails)

    response = await run_email_pass(agent, batches=3, size=1)

    assert response.status.value == "success" and response.result["batches_processed"] == 2     # e1, then e2, then none
    assert live_state(store) == before                                                          # no mark, no fact, live
    assert [e.email_id for e in await live_emails.get_unconsolidated_batch(USER)] == ["e1", "e2"]
    assert [(w["kind"], w["email_ids"]) for w in kit.emails.writes] == [("mark_consolidated", ["e1"]), ("mark_consolidated", ["e2"])]
    assert kit.report()["email_writes"] == {"mark_consolidated": 2}
    assert [r["pass"] for r in kit.rows] == ["email", "email"] and [p["pass"] for p in kit.passes] == ["email", "email"]


async def test_marked_emails_are_hidden_from_later_reads_of_the_same_run():
    store, live_emails = email_store()
    await live_emails.save_batch([email(f"e{i}", datetime(2026, 10, 10, i), datetime(2026, 10, 13, i)) for i in range(1, 4)])
    emails = NoWriteIndexedEmails(live_emails)

    first = await emails.get_unconsolidated_batch(USER, limit=2)
    await emails.mark_consolidated(USER, [e.email_id for e in first], datetime(2026, 10, 25, tzinfo=timezone.utc))
    second = await emails.get_unconsolidated_batch(USER, limit=2)

    assert [e.email_id for e in first] == ["e1", "e2"] and [e.email_id for e in second] == ["e3"]
    assert len(await live_emails.get_unconsolidated_batch(USER)) == 3
    assert await emails.get_unconsolidated_batch("another-user") == []


async def test_a_cluster_pass_that_refreshes_the_biographical_cache_writes_nothing_live(store, tmp_path):
    await seed(store, "walks daily and reads at night, and cooks on sundays")
    await seed(store, "cooks on sundays")
    before = live_state(store)
    agent, kit = await kit_agent(store, tmp_path, metered(calls(create("walks daily", expiry=EXPIRY_NONE))), answered(),
                                 live_repo=GuardedRepo(store.repo))
    message = AgentMessage(task_id="t", sender="s", recipient="r", intent=AgentIntent.DELEGATE,
                           payload={"task": "consolidate_cluster", "limit": 5},
                           context={"user_id": USER, "owner_timezone": "UTC"})

    with RequestContext(user_id=USER, account_id=ACC):
        response = await agent.execute(message)

    assert response.status.value == "success", response.error
    assert live_state(store) == before
    assert [w["kind"] for w in kit.repo.writes] == ["add_fact", "refresh_biographical_context_cache"]
    assert [(r["pass"], r["status"]) for r in kit.rows] == [("cluster", "created")]


# ----------------------------------------------------------------------------------------------- L2, L3: the ledger and the paths


def test_a_second_run_cannot_start_while_the_ledger_is_held_and_can_after_it_is_released(tmp_path):
    ledger = tmp_path / "spend.json"
    first = CostMeter(ledger)

    with pytest.raises(LedgerBusy, match="one bench at a time"):
        CostMeter(ledger)
    with pytest.raises(LedgerBusy):
        DryRunKit(ledger_path=ledger)
    first.close()
    DryRunKit(ledger_path=ledger).close()


def test_the_kit_refuses_any_path_outside_scripts_memory(tmp_path, monkeypatch):
    real_memory = kit_module.PROJECT_ROOT / "scripts" / "memory"
    monkeypatch.setattr(kit_module, "MEMORY_DIR", real_memory)

    with pytest.raises(PathOutsideMemory):
        CostMeter(tmp_path / "spend.json")
    with pytest.raises(PathOutsideMemory):
        CostMeter(real_memory / ".." / "spend.json")                     # an escape through ..
    outside = DryRunKit.__new__(DryRunKit)
    outside.meter = SimpleNamespace(close=lambda: None)
    with pytest.raises(PathOutsideMemory):
        DryRunKit.write_report(outside, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists() and not (tmp_path / "spend.json.lock").exists()


def test_a_report_path_inside_the_memory_directory_is_written(tmp_path):
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")

    written = kit.write_report(tmp_path / "nested" / "report.json")

    assert json.loads(written.read_text(encoding="utf-8"))["served_tokens"] == []
    kit.close()
