"""Facts S3 final review L1, L3, L4: the kit calls production's rules instead of copies, counts undated cleanup creates,
and keeps the owner's $30 cap.

The owner zone is compared with `ConsolidationService._owner_timezone` over the same fakes; the search hit is compared
field for field with the dict the real `FactManagementAdapter` returns from the real `SearchEnrichmentService`.
"""
import argparse
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from scripts.consolidation import s3_bench_support as support
from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import (
    DEFAULT_BUDGET_USD,
    BudgetAboveOwnerCap,
    DryRunKit,
    add_kit_arguments,
    check_budget_flag,
    derived_from_count,
    hit_from_document,
    owner_timezone,
)
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.request_context import RequestContext
from src.services.consolidation_service import ConsolidationService
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on
from tests.unit.scripts.test_s3_dry_run_kit import meta, seed
from tests.unit.services.test_consolidation_owner_timezone import ACCOUNT, Accounts, Users, account, profile


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


# ----------------------------------------------------------------------------------------------- L3: the owner zone

SCENARIOS = {
    "an owner in Tokyo": lambda: (Accounts(account(ann="owner")), Users(profile("ann", "Asia/Tokyo"))),
    "two owners": lambda: (Accounts(account(zed="owner", ann="owner")), Users(profile("ann", "Europe/Paris"), profile("zed", "Asia/Tokyo"))),
    "no account": lambda: (Accounts(), Users()),
    "no owner": lambda: (Accounts(account(bob="member")), Users(profile("bob", "Asia/Tokyo"))),
    "no profile": lambda: (Accounts(account(ann="owner")), Users()),
    "an invalid zone": lambda: (Accounts(account(ann="owner")), Users(profile("ann", "Mars/Olympus"))),
    "a failing account store": lambda: (Accounts(raises=RuntimeError("down")), Users()),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_kit_resolves_the_owner_zone_as_the_consolidation_service_does(name):
    accounts, users = SCENARIOS[name]()
    service = ConsolidationService.__new__(ConsolidationService)
    service._account_repo, service._user_repo = SCENARIOS[name]()

    assert await owner_timezone(accounts, users, ACCOUNT) == await service._owner_timezone(ACCOUNT)


async def test_a_zone_override_still_wins_in_the_kit_and_must_be_a_zone():
    accounts, users = SCENARIOS["an owner in Tokyo"]()

    assert await owner_timezone(accounts, users, ACCOUNT, override="Europe/Paris") == "Europe/Paris"
    assert accounts.asked == []
    with pytest.raises(ValueError):
        await owner_timezone(accounts, users, ACCOUNT, override="Mars/Olympus")


# ----------------------------------------------------------------------------------------------- L3: the search hit

def production_adapter(store: FactStore) -> FactManagementAdapter:
    enrichment = SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    return FactManagementAdapter(store.repo, store.embedder, store.write, enrichment)


async def hits_of(adapter: FactManagementAdapter, query: str, zone: str) -> Dict[str, Dict[str, Any]]:
    async with RequestContext(user_id=USER, account_id=ACC):
        found = await adapter.search_existing_facts(["walks"], query, limit=20, timezone=zone)
    return {hit["fact_id"]: hit for hit in found}


@pytest.mark.parametrize("expiry", [horizon(30), on("2026-12-24"), EXPIRY_NONE], ids=["horizon", "date", "none"])
@pytest.mark.parametrize("zone", ["Asia/Tokyo", "UTC"])
async def test_the_kit_search_hit_equals_the_production_hit_field_for_field(store, expiry, zone):
    fact_id = await seed(
        store, "walks daily in the park", expiry=expiry, tags=["walks", "health"], metadata={"place": "park"},
        reported_date="2026-10-22T23:30:00+00:00",
    )
    production = (await hits_of(production_adapter(store), "walks daily in the park", zone))[fact_id]
    doc = await store.repo.get_fact_by_id(fact_id)

    assert hit_from_document(doc, production, zone) == production


async def test_the_kit_hit_of_a_changed_document_equals_production_after_the_same_change(store):
    old_id = await seed(store, "walks daily in the park", expiry=EXPIRY_NONE, tags=["walks"], reported_date="2026-10-01T08:00:00+00:00")
    adapter = production_adapter(store)
    before = (await hits_of(adapter, "walks daily in the park", "Asia/Tokyo"))[old_id]
    changed = await store.adapter.update_fact(
        old_id, {"content": "walks daily in the park, now with the dog", "tags": ["walks", "dog"], "expiry": horizon(30)},
        ACC, context=ctx(actor="stage1", timezone="Asia/Tokyo"),
    )
    new_id = changed["fact_id"]
    after = (await hits_of(adapter, "walks daily in the park", "Asia/Tokyo"))[new_id]
    doc = await store.repo.get_fact_by_id(new_id)

    rendered = hit_from_document(doc, before, "Asia/Tokyo")

    assert new_id != old_id and rendered == {**after, "similarity": before["similarity"]}      # relevance stays the old hit's
    assert rendered["expiry"] == {"kind": "horizon", "days": 30} and "dog" in rendered["tags"]


# ----------------------------------------------------------------------------------------------- L1: undated creates

def row(pass_label: str, action: str, status: str, cleanup: bool, named: int, unreadable: bool = False) -> Dict[str, Any]:
    return {"pass": pass_label, "action": action, "status": status, "cleanup": cleanup, "derived_from_named": named,
            "derived_from_unreadable": unreadable, "reported_date_form": None}


async def test_the_report_counts_cleanup_creates_without_derived_from_per_pass_and_in_total(memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        kit.rows.extend([
            row("stage2a", "create", "created", True, 0),
            row("stage2a", "create", "created", True, 2),         # names its sources: dated from them
            row("stage2a", "create", "created", True, 1, unreadable=True),    # names ids no readable fact answers to
            row("stage2a", "create", "failed", True, 0),          # refused: nothing was dated
            row("stage2a", "update", "updated", True, 0),
            row("cluster", "create", "created", True, 0),
            row("cluster", "create", "created", True, 0),
            row("stage1", "create", "created", False, 0),         # a conversation pass: the model dates it
        ])

        report = kit.report()
    finally:
        kit.close()

    assert report["cleanup_creates_without_derived_from"] == {"per_pass": {"stage2a": 2, "cluster": 2}, "total": 4}


async def test_a_create_through_the_recorder_is_a_cleanup_row_with_its_derived_from_count(store, memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        source = await seed(store, "walks daily and reads at night")
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)
        cleanup = ctx(actor="stage2a", timezone="UTC", model_dates=False)

        await recorder.create_fact("walks daily", meta(), context=cleanup)
        await recorder.create_fact("reads at night", meta(derived_from=[source]), context=cleanup)
        await recorder.create_fact("a talk", meta(), context=ctx(actor="stage1", timezone="UTC"))

        assert [(r["cleanup"], r["derived_from_named"], r["status"]) for r in kit.rows] == [
            (True, 0, "created"), (True, 1, "created"), (False, 0, "created")]
        assert kit.report()["cleanup_creates_without_derived_from"] == {"per_pass": {"stage2a": 1}, "total": 1}
    finally:
        kit.close()


async def test_a_create_naming_only_unreadable_sources_counts_as_undated(store, memory_dir):
    kit = DryRunKit(ledger_path=memory_dir / "spend.json")
    try:
        readable = await seed(store, "walks daily and reads at night")
        kit.repo = kit_module.NoWriteFactRepository(store.repo)
        adapter = FactManagementAdapter(kit.repo, store.embedder, store.write, production_adapter(store)._search_enrichment)
        recorder = kit_module.WriteRecorder(adapter, kit)
        cleanup = ctx(actor="stage2a", timezone="UTC", model_dates=False)

        await recorder.create_fact("walks daily", meta(derived_from=["no-such-fact"]), context=cleanup)
        await recorder.create_fact("reads at night", meta(derived_from=["no-such-fact", readable]), context=cleanup)
        await recorder.create_fact("walks daily again", meta(derived_from=readable), context=cleanup)

        unreadable, partly, dated = kit.rows
        assert (unreadable["derived_from_named"], unreadable["derived_from_unreadable"]) == (1, True)
        assert "no readable fact" in unreadable["notes"]
        assert (partly["derived_from_named"], partly["derived_from_unreadable"]) == (2, False)      # one source was readable
        assert (dated["derived_from_named"], dated["derived_from_unreadable"]) == (1, False)
        assert kit.report()["cleanup_creates_without_derived_from"] == {"per_pass": {"stage2a": 1}, "total": 1}
    finally:
        kit.close()


@pytest.mark.parametrize("value,count", [(None, 0), ("", 0), (" ", 0), ("f1", 1), ([], 0), (["f1", "", "f2"], 2), (5, 0)])
def test_derived_from_count(value, count):
    assert derived_from_count(value) == count


def test_the_gate_lines_print_the_undated_count_next_to_g3():
    report = {"passes": [], "cleanup_creates_without_derived_from": {"per_pass": {"stage2a": 2, "cluster": 1}, "total": 3}}

    lines = support.gate_summary(report)

    g3 = next(i for i, line in enumerate(lines) if line.startswith("G3"))
    assert lines[g3 + 1].startswith("cleanup creates without derived_from: 3 (cluster 1, stage2a 2)")
    assert "restart from the batch end" in lines[g3 + 1]


def test_the_gate_lines_say_none_when_the_report_has_no_count():
    lines = support.gate_summary({"passes": []})

    assert any(line.startswith("cleanup creates without derived_from: 0 (none)") for line in lines)


# ----------------------------------------------------------------------------------------------- L4: the owner's cap

def test_a_budget_up_to_the_owners_cap_passes_and_a_higher_one_is_refused_without_the_flag():
    assert check_budget_flag(DEFAULT_BUDGET_USD) == DEFAULT_BUDGET_USD == 30.0
    assert check_budget_flag(5.0) == 5.0
    with pytest.raises(BudgetAboveOwnerCap, match="owner's call"):
        check_budget_flag(30.01)
    with pytest.raises(BudgetAboveOwnerCap):
        check_budget_flag(300)


def test_the_explicit_flag_lets_a_higher_budget_through():
    assert check_budget_flag(100.0, allow_over_owner_cap=True) == 100.0


def test_the_flags_parse_and_the_over_cap_flag_is_off_by_default():
    parser = argparse.ArgumentParser()
    add_kit_arguments(parser)

    plain = parser.parse_args([])
    raised = parser.parse_args(["--budget-usd", "100", "--allow-over-owner-cap"])

    assert (plain.budget_usd, plain.allow_over_owner_cap) == (30.0, False)
    assert (raised.budget_usd, raised.allow_over_owner_cap) == (100.0, True)


async def test_a_kit_session_over_the_cap_is_refused_before_any_ledger_lock(memory_dir, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", memory_dir / "spend.json")
    args = SimpleNamespace(budget_usd=300.0, timezone=None, allow_over_owner_cap=False)

    with pytest.raises(BudgetAboveOwnerCap):
        async with support.kit_session(SimpleNamespace(), args, USER, ACC, "x"):
            raise AssertionError("the body must not run")

    assert not (memory_dir / "spend.json.lock").exists()
