"""The Cabinet on the fact profile (RFC §6 row 9, §3.2): `GET /api/user/fact-domains` (every registry key once, in
Cabinet order, labelled in the page language, behind the same session gate as the other Cabinet routes), the facts
list filtered by profile `domain_keys`, and the fields the badge reads (`primary_domain`; the old `domain` while a fact
has none). Real routes and repository on the evaluating Firestore fake (index refusal on); the page itself is read as
text for the parts only it holds."""
import copy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain import fact_domains
from src.domain.entities import FactState
from src.domain.fact_domains import Domain
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, OTHER, USER, make_fact
from tests.support.fact_lifecycle_world import make_fact_world

TOKEN = {"Authorization": "Bearer t"}
PAGE = Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "cabinet.html"


def _app(repo: Any = None, verify=None) -> Quart:
    session_service = MagicMock()
    session_service.verify_access_token = verify or MagicMock(
        return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_fact_domains")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(),
        fact_repo=repo if repo is not None else MagicMock(), embedding_service=MagicMock(),
        connectors=ConnectorRegistry(),
    ))
    return app


async def _get(path: str, repo: Any = None, headers=TOKEN, verify=None):
    async with _app(repo, verify).test_client() as client:
        resp = await client.get(path, headers=headers)
        return resp.status_code, await resp.get_json()


@pytest.fixture
def world():
    store, repo, _ = make_fact_world()
    return store, repo


YOGA = dict(primary_domain=Domain.SPORT, domain_keys=[Domain.SPORT])
INSURANCE = dict(primary_domain=Domain.TRANSPORT, domain_keys=[Domain.TRANSPORT, Domain.MONEY])


# --------------------------------------------------------------------------------------------- the domains route

async def test_the_route_lists_every_registry_key_once_in_cabinet_order():
    status, body = await _get("/api/user/fact-domains")

    assert status == 200
    keys = [d["key"] for d in body["domains"]]
    assert keys == list(fact_domains.cabinet_keys())
    assert sorted(keys) == sorted(fact_domains.all_keys()) and len(set(keys)) == len(keys)
    orders = [d["order"] for d in body["domains"]]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)


async def test_without_a_language_the_labels_are_english():
    _, body = await _get("/api/user/fact-domains")

    assert {d["key"]: d["label"] for d in body["domains"]} == {
        k: fact_domains.domain_entry(k).labels["en"] for k in fact_domains.all_keys()}


async def test_the_labels_follow_the_page_language():
    _, body = await _get("/api/user/fact-domains?lang=uk")

    labels = {d["key"]: d["label"] for d in body["domains"]}
    assert labels[Domain.TRANSPORT] == "Транспорт" and labels[Domain.SPORT] == "Спорт"
    assert labels == {k: fact_domains.domain_entry(k).labels["uk"] for k in fact_domains.all_keys()}


async def test_a_language_without_labels_falls_back_to_english():
    _, body = await _get("/api/user/fact-domains?lang=ja")

    assert {d["key"]: d["label"] for d in body["domains"]}[Domain.TRANSPORT] == "Transport"
    assert all(d["label"] for d in body["domains"])


async def test_the_route_refuses_a_request_without_a_session():
    status, _ = await _get("/api/user/fact-domains", headers={})

    assert status == 401


async def test_the_route_refuses_an_invalid_session_as_its_neighbours_do():
    verify = MagicMock(side_effect=ValueError("expired"))

    assert (await _get("/api/user/fact-domains", verify=verify))[0] == 401
    assert (await _get("/api/user/facts/browse", verify=verify))[0] == 401


# ------------------------------------------------------------------------------------------- the domain filter

async def test_the_filter_lists_the_facts_whose_domain_keys_hold_the_key(world):
    _, repo = world
    await repo.add_fact(make_fact("morning yoga 30 min", age_days=1, **YOGA))
    await repo.add_fact(make_fact("Alex's car insurance with Example Mutual Insurance", age_days=2, **INSURANCE))
    await repo.add_fact(make_fact("plays tennis on Sundays", age_days=3))  # no profile

    _, money = await _get(f"/api/user/facts/browse?domain={Domain.MONEY}", repo)
    _, sport = await _get(f"/api/user/facts/browse?domain={Domain.SPORT}", repo)
    _, everything = await _get("/api/user/facts/browse", repo)

    assert [f["text"] for f in money["facts"]] == ["Alex's car insurance with Example Mutual Insurance"]
    assert [f["text"] for f in sport["facts"]] == ["morning yoga 30 min"]
    assert len(everything["facts"]) == 3


async def test_the_filter_narrows_the_stale_list_and_pages(world):
    _, repo = world
    for n in range(3):
        await repo.add_fact(make_fact(f"morning yoga 30 min, week {n}", age_days=n, state=FactState.STALE, **YOGA))
    await repo.add_fact(make_fact("Alex's car insurance with Example Mutual Insurance", state=FactState.STALE,
                                  **INSURANCE))
    await repo.add_fact(make_fact("morning yoga 30 min, current", **YOGA))

    _, first = await _get(f"/api/user/facts/browse?state=stale&limit=2&domain={Domain.SPORT}", repo)
    _, second = await _get(
        f"/api/user/facts/browse?state=stale&limit=2&domain={Domain.SPORT}&cursor={first['next_cursor']}", repo)

    assert [f["text"] for f in first["facts"] + second["facts"]] == [
        "morning yoga 30 min, week 2", "morning yoga 30 min, week 1", "morning yoga 30 min, week 0"]
    assert second["next_cursor"] is None


async def test_the_filter_lists_only_the_sessions_account(world):
    _, repo = world
    await repo.add_fact(make_fact("morning yoga 30 min", **YOGA))
    await repo.add_fact(make_fact("morning yoga 30 min, theirs", account=OTHER, **YOGA))

    _, body = await _get(f"/api/user/facts/browse?domain={Domain.SPORT}", repo)

    assert [f["text"] for f in body["facts"]] == ["morning yoga 30 min"]


@pytest.mark.parametrize("value", ["finance", "agent_directive", "Sport", "astrology"])
async def test_a_value_that_is_no_registry_key_is_a_400_and_nothing_is_read(world, value):
    store, repo = world
    await repo.add_fact(make_fact("Alex's car insurance with Example Mutual Insurance", **INSURANCE))
    before = copy.deepcopy(store.docs)

    status, body = await _get(f"/api/user/facts/browse?domain={value}", repo)

    assert status == 400 and "domain" in body["error"]
    assert store.docs == before


async def test_the_dry_run_kit_forwards_the_domain_key_to_the_live_store(world):
    from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
    _, repo = world
    await repo.add_fact(make_fact("morning yoga 30 min", **YOGA))
    await repo.add_fact(make_fact("Alex's car insurance with Example Mutual Insurance", **INSURANCE))

    facts, _ = await NoWriteFactRepository(repo).get_paginated_facts(ACC, domain_key=Domain.TRANSPORT)

    assert [f.text for f in facts] == ["Alex's car insurance with Example Mutual Insurance"]


# ------------------------------------------------------------------------------------------- the badge fields

async def test_browse_carries_the_profile_domain_and_the_old_domain_while_it_is_absent(world):
    _, repo = world
    await repo.add_fact(make_fact("morning yoga 30 min", age_days=2, **YOGA))
    await repo.add_fact(make_fact("plays tennis on Sundays", age_days=1))

    _, body = await _get("/api/user/facts/browse", repo)

    rows = {f["text"]: (f["primary_domain"], f["domain"]) for f in body["facts"]}
    assert rows == {"morning yoga 30 min": (Domain.SPORT, "preference"),
                    "plays tennis on Sundays": (None, "preference")}


async def test_the_recently_closed_list_carries_the_profile_domain(world):
    from datetime import datetime, timedelta, timezone
    _, repo = world
    closed_at = datetime.now(timezone.utc) - timedelta(days=1)
    await repo.add_fact(make_fact(
        "Alex's car insurance with Example Mutual Insurance", state=FactState.ARCHIVED, is_current=False,
        valid_to=closed_at, **INSURANCE,
        lifecycle_events=[{"at": closed_at, "from": "current", "to": "archived", "actor": "review", "reason": "t"}]))

    _, body = await _get("/api/user/facts/recently-closed", repo)

    [row] = body["facts"]
    assert (row["primary_domain"], row["domain"]) == (Domain.TRANSPORT, "finance")


async def test_the_search_list_carries_the_profile_domain(world):
    store, repo = world
    await repo.add_fact(make_fact("morning yoga 30 min", **YOGA))
    await repo.add_fact(make_fact("plays tennis on Sundays"))
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": ACC, "role": "owner"})
    app = Quart("test_cabinet_fact_domains_search")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=repo,
        embedding_service=store.embedder, connectors=ConnectorRegistry(),
    ))

    async with app.test_client() as client:
        resp = await client.post("/api/user/facts/search", headers=TOKEN, json={"query": "morning yoga 30 min"})
        body = await resp.get_json()

    assert resp.status_code == 200
    rows = {f["text"]: (f["primary_domain"], f["domain"]) for f in body["facts"]}
    assert rows["morning yoga 30 min"] == (Domain.SPORT, "preference")
    assert rows["plays tennis on Sundays"] == (None, "preference")


# ------------------------------------------------------------------------------------------------------- the page

@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_page_has_no_domain_map_of_its_own(page):
    assert "DOMAIN_LABELS" not in page and "browseKnownDomains" not in page
    for old in ('biographical: "Biographical"', 'possession: "Possession"', 'financial: "Financial"'):
        assert old not in page


def test_the_page_loads_labels_and_filter_options_from_the_route_in_its_language(page):
    assert "/api/user/fact-domains?lang=${encodeURIComponent(lang)}" in page
    assert "const lang = _currentLang;" in page
    assert "...factDomains.map((d) => ({ label: d.label, value: d.key }))" in page


def test_the_badge_reads_the_profile_domain_then_the_old_domain(page):
    assert "if (f.primary_domain) return domainLabel(f.primary_domain);" in page
    assert 'return f.domain ? f.domain.replace(/_/g, " ") : "";' in page
    assert '<span class="fact-domain-badge">${escapeHtml(badge)}</span>' in page
