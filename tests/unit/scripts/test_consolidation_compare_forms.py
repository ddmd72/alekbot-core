"""The email dry run's compare mode (email index v2 §6.7, slice 9): the same carded mail goes through Stage 3 once as
one-line candidates and once as cards, each on its own kit session. The live mail is the evaluating fake behind the real
repository and card store; the agent is the real one under a scripted model. Both runs serve the same mail, no write
reaches the live store, the card run's token, profile and blueprint come from the snapshot, and an empty set spends
nothing."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import NoWriteIndexedEmails, NoWriteMemoryBatches
from scripts.email import test_consolidation_dryrun as bench5
from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect, pending_fields, settled_fields
from src.domain.email_memory_candidate import EMAIL_CANDIDATES_HEADER, SETTLED_FOR_MEMORY
from src.domain.prompt_v3.token import TokenId
from tests.support.fact_kit import ACC, USER
from tests.support.firestore_eval import eval_transactions
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail
from tests.unit.scripts.test_s3_benches_route import bench_agent, handles_for
from tests.unit.scripts.test_s3_dry_run_kit import (  # noqa: F401  (fixtures: store, memory_dir)
    AllowAll,
    answered,
    email_store,
    memory_dir,
    store,
)

OLD = datetime(2026, 10, 1, 8, 0)


def email(email_id: str, minutes: int) -> IndexedEmail:
    return IndexedEmail(email_id=email_id, user_id=USER, account_id=ACC, source="gmail", text=f"Fact of {email_id}.",
                        tags=["mail"], category="travel", metadata={}, subject=email_id, from_address="a@example.com",
                        email_date=OLD, indexed_at=OLD + timedelta(minutes=minutes))


def card_fields(email_id: str):
    m = mail(email_id=email_id)
    card = build_card(parse_mail_answer(answer_json()), m, [],
                      build_card_input(m, BOOKING_BODY, [], [], direct=True, nonce=NONCE).evidence, PEOPLE)
    return settled_fields(card, digest="dg", model="flash-lite", now=datetime.now(timezone.utc), vector_pending=False)


@pytest.fixture
def live():
    with eval_transactions():
        fact_store, emails = email_store()
        env = SimpleNamespace(domain_email_facts_collection="eval_email_facts",
                              email_indexing_state_collection="eval_email_state")
        yield SimpleNamespace(store=fact_store, emails=emails, cards=FirestoreEmailCardStore(fact_store.db, env))


async def seed(live) -> None:
    await live.emails.save_batch([email("c1", 1), email("c2", 2), email("pending", 3), email("plain", 4)])
    for email_id in ("c1", "c2"):
        await live.cards.write_card(USER, email_id, card_fields(email_id), None, CardExpect.absent())
    await live.cards.write_card(USER, "pending", pending_fields(reason="outage", failures=0, model=""), None,
                                CardExpect.absent())


def builder(live, built):
    async def build():
        agent = bench_agent(live.store, answered(), email_repo=live.emails)
        agent.prompt_builder.assembly_service.blueprint_repo = SimpleNamespace()    # the live one; never read here
        agent.prompts_built = []

        async def build_for_agent(*, agent_type, **_):          # records the profile each prompt is built for
            agent.prompts_built.append(agent_type)
            return "SYSTEM"

        agent.prompt_builder.build_for_agent = build_for_agent
        built.append(agent)
        return handles_for(agent, live.store)
    return build


def args(**over):
    return SimpleNamespace(user_id=USER, account_id=ACC, budget_usd=30.0, timezone="Europe/Madrid", limit=10, **over)


def live_docs(live):
    return json.dumps(live.store.db.collection("eval_email_facts").docs, default=str, sort_keys=True)


async def test_both_runs_serve_the_same_carded_mail_and_nothing_reaches_the_live_store(live, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    await seed(live)
    before, built = live_docs(live), []

    path = await bench5.compare_forms(builder(live, built), args())

    report = json.loads(Path(path).read_text(encoding="utf-8"))
    assert report["email_ids"] == ["c1", "c2"]                                  # carded and readable only
    assert report["forms"]["line"]["email_ids_read"] == report["forms"]["card"]["email_ids_read"] == ["c1", "c2"]
    line_agent, card_agent = built
    line_first, card_first = line_agent.requests[0], card_agent.requests[0]
    assert "[system_alert]" in line_first and EMAIL_CANDIDATES_HEADER not in line_first
    assert card_first.startswith(EMAIL_CANDIDATES_HEADER)
    for first in (line_first, card_first):
        shown = [json.loads(x.split(". ", 1)[1])["email_id"] for x in first.splitlines() if re.match(r"^\d+\. \{", x)]
        assert shown == ["c1", "c2"]
    assert live_docs(live) == before                                            # no mark, no card, no fact, live
    assert live.store.docs == {}
    assert sorted(p.name.split("_")[4] for p in tmp_path.glob("s3_dry_run_compare_*.json")) == ["card", "line"]


async def test_the_card_run_builds_its_prompt_for_the_email_profile(live, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    await seed(live)
    built = []

    await bench5.compare_forms(builder(live, built), args())

    assert [a.prompts_built for a in built] == [["consolidation"], ["consolidation_email"]]


async def test_an_empty_set_says_so_and_spends_nothing(live, tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "SPEND_LEDGER", tmp_path / "spend.json")
    await live.emails.save_batch([email("plain", 1)])

    assert await bench5.compare_forms(builder(live, []), args()) is None

    assert not (tmp_path / "spend.json").exists() and list(tmp_path.glob("*.json")) == []


async def test_the_overlays_serve_the_three_new_ids_from_the_snapshot(store):
    class Refuse:
        async def get(self, *a):
            raise AssertionError("read from the live store")

        async def get_agent_profile(self, agent_id):
            raise AssertionError("read from the live store")

    service = SimpleNamespace(token_repo=Refuse(), profile_repo=Refuse(), blueprint_repo=Refuse(),
                              security_port=AllowAll(), cleared=0)
    service.invalidate_cache = lambda: setattr(service, "cleared", service.cleared + 1)

    bench5.serve_card_prompt(service)

    token = await service.token_repo.get(TokenId("CONSOLIDATION_EMAIL_CANDIDATES"))
    profile = await service.profile_repo.get_agent_profile("consolidation_email")
    blueprint = await service.blueprint_repo.get("consolidation_email_v1")
    assert token.class_ == "email_candidates" and "Email_Candidates" in token.content
    assert profile.blueprint_id == "consolidation_email_v1" and "CONSOLIDATION_EMAIL_CANDIDATES" in profile.tokens
    assert blueprint.class_order[-1] == "email_candidates" and service.cleared == 1


async def test_the_kit_hides_the_mail_its_run_marked_from_the_card_reads(live):
    await seed(live)
    marks = NoWriteIndexedEmails(live.emails)
    batches = NoWriteMemoryBatches(live.emails, marks)
    stale = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    first = await batches.memory_batch(USER, 2, SETTLED_FOR_MEMORY, stale)
    await marks.mark_consolidated(USER, [m.email.email_id for m in first], datetime.now(timezone.utc))
    second = await batches.memory_batch(USER, 2, SETTLED_FOR_MEMORY, stale)

    assert [m.email.email_id for m in first] == ["c1", "c2"]
    assert [m.email.email_id for m in second] == ["pending", "plain"]
    assert len(await live.emails.memory_batch(USER, 10, SETTLED_FOR_MEMORY, stale)) == 4
