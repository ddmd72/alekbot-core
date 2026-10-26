"""Facts S4 · T12a: the dry-run kit can run the review without touching anything live.

Covered: the snapshot overlays that serve the review's lifecycle token, profile and blueprint (and the real prompt assembly
over them); `prepare` for the review; the stand-ins (question store, lease, task queue, sessions, platform, notifier,
channel, language); `stage_stale`; and an owner's answer through the real `FactAnswerService` on the kit.

The live side is the review world: the real repository and the real review store on one evaluating Firestore fake. A
guard refuses every live fact write; "nothing live" is checked as that fake's documents and change stamps staying
byte-identical.
"""
import copy
import hashlib
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from google.cloud.firestore import FieldFilter

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.review_kit_stand_ins import (
    KitLanguagePreferences,
    LiveEffectRefused,
    MemoryLease,
    NoAppendSessions,
    NoPostNotifier,
    NoopAgentFactory,
    RecordingInteractive,
    RecordingTaskQueue,
)
from scripts.consolidation.review_kit_store import RecordingReviewStore
from scripts.consolidation.s3_dry_run_kit import (
    REVIEW_TOKEN_IDS,
    S3_TOKEN_IDS,
    DryRunKit,
    NoWriteFactRepository,
    SnapshotBlueprintOverlay,
    SnapshotProfileOverlay,
    SnapshotTokenError,
    SnapshotTokenOverlay,
    WriteRecorder,
)
from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.billing import BillingAccount
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.entities import FactState
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_question_close import QuestionClose
from src.domain.fact_review_reasons import answer_reason
from src.domain.fact_update import parse_fact_update
from src.domain.language import LanguageCode
from src.domain.llm import Message, MessagePart
from src.domain.fact_answer_result import AnswerResult
from src.domain.messaging import MessageRef
from src.domain.prompt_v3.blueprint import Blueprint
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId
from src.domain.user import UserBotConfig, UserProfile
from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.services.prompt_v3.prompt_assembly_service import PromptAssemblyService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_store import FactStore, INDEXES_FILE
from tests.support.fact_write import ctx, rendered
from tests.unit.scripts.test_s3_dry_run_kit import (
    AllowAll,
    FakeProfiles,
    HeldTokens,
    SONNET,
    answered,
    assembly,
    held,
    scripted,
    snapshot_body,
)
from tests.unit.scripts.test_s3_dry_run_kit_production_rules import production_adapter
from tests.unit.scripts.test_s3_dry_run_kit_review_reads import GuardedRepo
from tests.unit.services.test_fact_answer_service import question_on, stale_fact
from tests.unit.agents.test_consolidation_write_context import make_agent

LISBON = "Europe/Lisbon"
LIFECYCLE = "CONSOLIDATION_LIFECYCLE"


@pytest.fixture(autouse=True)
def memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(kit_module, "MEMORY_DIR", tmp_path)
    return tmp_path


def live_state(world: ReviewWorld) -> Dict[str, Any]:
    """Every collection of the world's fake with its raw documents and change stamps: what 'byte-identical' means."""
    return {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in world.store.db.cols.items() if col.docs}


class Accounts:
    async def get_account(self, account_id: str) -> BillingAccount:
        return BillingAccount(account_id=account_id, iam_policy={USER: "owner"})


class Users:
    async def get_user(self, user_id: str) -> UserProfile:
        return UserProfile(user_id=user_id, account_id=ACC, config=UserBotConfig(timezone=LISBON))


class RefusingBlueprints(BlueprintRepository):
    """The live blueprint store: the review's blueprint is not in it before the rollout, so nothing may ask it."""

    async def get(self, blueprint_id: str) -> Blueprint:
        raise KeyError(f"Blueprint not found: {blueprint_id}")

    async def list_all(self) -> List[Blueprint]:
        return [Blueprint(id="other_v1", outer_class="Other extends Agent", class_order=["policies"])]

    async def save(self, blueprint: Blueprint) -> None:
        raise AssertionError("a live blueprint write")

    async def delete(self, blueprint_id: str) -> None:
        raise AssertionError("a live blueprint delete")

    async def exists(self, blueprint_id: str) -> bool:
        return False


class EmptyProfiles(AgentProfileRepository):
    async def get_agent_profile(self, agent_id: str):
        raise KeyError(agent_id)

    async def get_override_tokens(self, owner_type, owner_id):
        return {}

    async def set_override_tokens(self, owner_type, owner_id, tokens, clear_ids=None) -> None:
        raise AssertionError("a live profile write")

    async def delete_profile(self, owner_type, owner_value) -> None:
        raise AssertionError("a live profile delete")


def snapshot_copy(tmp_path: Path) -> Path:
    """A copy of the prompt snapshot a test may edit."""
    target = tmp_path / "snapshot"
    shutil.copytree(kit_module.SNAPSHOT_DIR, target)
    return target


# ---------------------------------------------------------------------------------------------- the snapshot overlays

async def test_the_profile_overlay_serves_the_snapshot_review_profile_and_leaves_the_rest_to_the_live_store():
    overlay = SnapshotProfileOverlay(FakeProfiles(), kit_module.SNAPSHOT_DIR)

    profile = await overlay.get_agent_profile("consolidation_review")

    assert profile.blueprint_id == "consolidation_review_v1"
    assert sorted(profile.tokens) == ["CONSOLIDATION_LIFECYCLE", "CONSOLIDATION_TAXONOMY"]
    assert profile.tokens["CONSOLIDATION_TAXONOMY"].order < profile.tokens[LIFECYCLE].order
    assert all(token.non_overridable for token in profile.tokens.values())
    assert (await overlay.get_agent_profile("consolidation")).blueprint_id == "consolidation_agent_v1"
    mine = {"MY": ProfileToken.from_dict("MY", {"order": 1})}
    assert await overlay.get_override_tokens(OwnerType.USER, USER) == {}
    with pytest.raises(PermissionError):
        await overlay.set_override_tokens(OwnerType.USER, USER, mine)
    with pytest.raises(PermissionError):
        await overlay.delete_profile(OwnerType.AGENT, "consolidation_review")


async def test_the_blueprint_overlay_serves_the_snapshot_review_blueprint_and_leaves_the_rest_to_the_live_store():
    overlay = SnapshotBlueprintOverlay(RefusingBlueprints(), kit_module.SNAPSHOT_DIR)

    blueprint = await overlay.get("consolidation_review_v1")

    assert blueprint.class_order == ["taxonomy", "lifecycle"]
    assert blueprint.outer_class == "ConsolidationReviewAgent extends Agent"
    assert await overlay.exists("consolidation_review_v1") and not await overlay.exists("other_v1")
    assert sorted(b.id for b in await overlay.list_all()) == ["consolidation_review_v1", "other_v1"]
    with pytest.raises(KeyError):
        await overlay.get("other_v1")
    with pytest.raises(PermissionError):
        await overlay.save(blueprint)
    with pytest.raises(PermissionError):
        await overlay.delete("consolidation_review_v1")


async def test_a_snapshot_without_the_review_files_is_a_key_error_not_an_empty_prompt(tmp_path):
    with pytest.raises(KeyError):
        await SnapshotProfileOverlay(EmptyProfiles(), tmp_path).get_agent_profile("consolidation_review")
    with pytest.raises(KeyError):
        await SnapshotBlueprintOverlay(RefusingBlueprints(), tmp_path).get("consolidation_review_v1")


async def test_the_real_assembly_over_the_three_overlays_renders_the_review_prompt_from_the_snapshot():
    tokens = SnapshotTokenOverlay(HeldTokens(), AllowAll(), kit_module.SNAPSHOT_DIR, (*S3_TOKEN_IDS, *REVIEW_TOKEN_IDS))
    service = PromptAssemblyService(
        tokens, SnapshotBlueprintOverlay(RefusingBlueprints()), SnapshotProfileOverlay(EmptyProfiles()),
        AllowAll(), None, None,    # type: ignore[arg-type]    # the static template uses neither formatter
    )

    prompt = await service._assemble_static_template("consolidation_review", ACC, USER)

    lifecycle_line = max((line.strip() for line in snapshot_body(LIFECYCLE).splitlines()), key=len)
    taxonomy_line = max((line.strip() for line in snapshot_body("CONSOLIDATION_TAXONOMY").splitlines()), key=len)
    assert prompt.startswith("class ConsolidationReviewAgent extends Agent {")
    assert prompt.index("    taxonomy {") < prompt.index(taxonomy_line) < prompt.index("    lifecycle {") < prompt.index(lifecycle_line)


async def test_the_token_overlay_serves_the_lifecycle_token_only_when_asked_to():
    live = HeldTokens(held("CONSOLIDATION_OUTPUT_SPEC"), held(LIFECYCLE, "from firestore"))

    plain = SnapshotTokenOverlay(live, AllowAll())
    review = SnapshotTokenOverlay(live, AllowAll(), kit_module.SNAPSHOT_DIR, (*S3_TOKEN_IDS, *REVIEW_TOKEN_IDS))

    assert (await plain.get(TokenId(LIFECYCLE))).content == "from firestore"
    assert (await review.get(TokenId(LIFECYCLE))).content == snapshot_body(LIFECYCLE)


# ---------------------------------------------------------------------------------------------- prepare, with the review

async def review_kit(store: FactStore, tmp_path: Path, *, snapshot_dir=None, tokens=None, profile_options=None):
    """A kit `with_review` installed on a consolidation agent whose assembly service is the stub of the S3 kit tests."""
    agent = make_agent(store, scripted(answered()))
    agent.model_name = SONNET
    service = assembly(tokens, **(profile_options or {}))
    service.blueprint_repo = RefusingBlueprints()
    agent.prompt_builder.assembly_service = service
    kit = DryRunKit(
        ledger_path=tmp_path / "spend.json", snapshot_dir=snapshot_dir or kit_module.SNAPSHOT_DIR, with_review=True,
    )
    kit.timezone = "UTC"
    kit.install(agent)
    return agent, kit


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def test_prepare_for_the_review_serves_the_lifecycle_token_and_reports_its_hash_beside_the_five(store, tmp_path):
    agent, kit = await review_kit(store, tmp_path)

    await kit.prepare(USER, ACC)

    report = kit.report()
    assert report["review_tokens"] == [{"id": LIFECYCLE, "sha256": hashlib.sha256(snapshot_body(LIFECYCLE).encode()).hexdigest()}]
    assert [t["id"] for t in report["served_tokens"]] == list(S3_TOKEN_IDS)
    assert isinstance(agent.prompt_builder.assembly_service.profile_repo, SnapshotProfileOverlay)
    kit.close()


async def test_a_kit_without_the_review_leaves_the_profile_and_blueprint_stores_alone(store, tmp_path):
    agent = make_agent(store, scripted(answered()))
    service = assembly()
    service.blueprint_repo = RefusingBlueprints()
    live_profiles, live_blueprints = service.profile_repo, service.blueprint_repo
    agent.prompt_builder.assembly_service = service
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")
    kit.install(agent)

    await kit.prepare(USER, ACC)

    assert (service.profile_repo, service.blueprint_repo) == (live_profiles, live_blueprints)
    assert kit.report()["review_tokens"] == [] and kit.report()["review"] is None
    kit.close()


def edited(snapshot: Path, relative: str, old: str, new: str) -> None:
    path = snapshot / relative
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


async def test_a_review_profile_that_does_not_list_the_lifecycle_token_is_refused(store, tmp_path):
    snapshot = snapshot_copy(tmp_path)
    edited(snapshot, "profiles/consolidation_review.yaml", "  CONSOLIDATION_LIFECYCLE:\n    non_overridable: true\n    order: 20\n", "")
    agent, kit = await review_kit(store, tmp_path, snapshot_dir=snapshot)

    with pytest.raises(SnapshotTokenError, match="consolidation_review profile does not list CONSOLIDATION_LIFECYCLE"):
        await kit.prepare(USER, ACC)
    kit.close()


async def test_a_review_blueprint_with_no_section_for_the_lifecycle_token_is_refused(store, tmp_path):
    snapshot = snapshot_copy(tmp_path)
    edited(snapshot, "blueprints/consolidation_review_v1.yaml", "- lifecycle\n", "")
    agent, kit = await review_kit(store, tmp_path, snapshot_dir=snapshot)

    with pytest.raises(SnapshotTokenError, match="no section for CONSOLIDATION_LIFECYCLE"):
        await kit.prepare(USER, ACC)
    kit.close()


async def test_a_missing_lifecycle_token_file_fails_before_any_model_call(store, tmp_path):
    snapshot = snapshot_copy(tmp_path)
    (snapshot / "tokens" / "system" / f"{LIFECYCLE}.groovy").unlink()
    agent, kit = await review_kit(store, tmp_path, snapshot_dir=snapshot)

    with pytest.raises(SnapshotTokenError, match=LIFECYCLE):
        await kit.prepare(USER, ACC)
    kit.close()


def override_of_lifecycle() -> Token:
    return Token(id=TokenId("MY_LIFECYCLE"), category=TokenCategory("lifecycle"), class_=TokenClass("lifecycle"),
                 content="mine", metadata={})


async def test_an_override_the_review_profile_blocks_is_recorded_and_one_it_allows_is_refused(store, tmp_path):
    tokens = HeldTokens(held("CONSOLIDATION_OUTPUT_SPEC"), override_of_lifecycle())
    overrides = {OwnerType.ACCOUNT: {"MY_LIFECYCLE": ProfileToken.from_dict("MY_LIFECYCLE", {"order": 1})}}
    agent, kit = await review_kit(store, tmp_path, tokens=tokens, profile_options={"overrides": overrides})

    await kit.prepare(USER, ACC)

    report = kit.report()
    assert report["review_overrides_blocked"] == [{"level": "account", "override": "MY_LIFECYCLE", "replaces": LIFECYCLE}]
    assert report["overrides_blocked"] == []
    kit.close()

    snapshot = snapshot_copy(tmp_path)
    edited(snapshot, "profiles/consolidation_review.yaml",
           "  CONSOLIDATION_LIFECYCLE:\n    non_overridable: true", "  CONSOLIDATION_LIFECYCLE:\n    non_overridable: false")
    agent, kit = await review_kit(store, tmp_path, snapshot_dir=snapshot, tokens=tokens,
                                  profile_options={"overrides": overrides})
    with pytest.raises(SnapshotTokenError, match="account override .*MY_LIFECYCLE.*CONSOLIDATION_LIFECYCLE"):
        await kit.prepare(USER, ACC)
    kit.close()


# ---------------------------------------------------------------------------------------------- the question store

def guarded_kit(world: ReviewWorld, tmp_path: Path) -> DryRunKit:
    """A kit over the world's facts: overlay store, the real adapter and the recorder, no agent."""
    kit = DryRunKit(ledger_path=tmp_path / "spend.json")
    kit.repo = NoWriteFactRepository(GuardedRepo(world.repo))
    adapter = FactManagementAdapter(
        kit.repo, world.store.embedder, world.store.write, production_adapter(world.store)._search_enrichment)
    kit.recorder = WriteRecorder(adapter, kit)
    return kit


async def test_the_kit_store_runs_the_production_adapter_so_it_ends_where_the_live_adapter_does():
    async def seeded() -> ReviewWorld:
        world = ReviewWorld()
        await world.seed_question(question_id="q1", fact_id="f1", status=QuestionStatus.QUEUED)
        await world.seed_question(question_id="q2", fact_id="f2", status=QuestionStatus.QUEUED,
                                  queue_key=NOW - timedelta(days=20))
        return world

    live, twin = await seeded(), await seeded()
    kit_store = RecordingReviewStore()
    await kit_store.load_account(twin.review_store, "acc")
    ref = MessageRef(platform="slack", channel_id="D1", message_id="m1")
    day = NOW.date()

    async def script(store):
        first = await store.claim_next("acc", day, "u1:D1")
        await store.mark_appended(first.question_id, NOW)
        await store.set_message_ref(first.question_id, ref)
        await store.mark_asked(first.question_id, NOW)
        await store.set_offers("acc", {"f9": 2})
        assert await store.claim_next("acc", day, "u1:D1") is None          # one is out, and today's sends are used
        closed = await store.close_and_release(first.question_id, ClosedBy.RESOLVED_ELSEWHERE, None, None, NOW)
        await store.mark_started("acc", NOW)
        await store.mark_succeeded("acc", NOW)
        second = await store.claim_next("acc", day + timedelta(days=1), "u1:D1")
        return first.question_id, closed, second.question_id

    assert await script(live.review_store) == await script(kit_store) == ("q1", QuestionClose.RELEASED, "q2")
    live_questions = {q.question_id: q for q in await live.review_store.get_questions(["q1", "q2"])}
    assert {q.question_id: q for q in kit_store.questions()} == live_questions
    assert kit_store.state("acc") == await live.review_store.get_state("acc")


async def test_the_kit_store_reads_the_live_account_and_writes_nothing_back():
    world = ReviewWorld()
    await world.seed_state(sent=1, sent_on=NOW.date(), current_question_id="q1")
    await world.seed_question(status=QuestionStatus.ASKED, question_id="q1")
    await world.seed_question(status=QuestionStatus.QUEUED, question_id="q2", fact_id="f2")
    before = live_state(world)
    store = RecordingReviewStore()
    await store.load_account(world.review_store, "acc")
    assert store.writes == []                                              # loading is not writing

    assert await store.close_and_release("q1", ClosedBy.RESOLVED_ELSEWHERE, None, None, NOW) is QuestionClose.RELEASED
    assert await store.create_queued(next(q for q in store.questions() if q.question_id == "q2")) is False   # exists
    claimed = await store.claim_next("acc", NOW.date() + timedelta(days=1), "u1:D1")

    assert claimed.question_id == "q2" and store.state("acc").current_question_id == "q2"
    assert [w["doc_id"] for w in store.writes] == ["q1", "acc", "q2", "acc"]
    assert live_state(world) == before
    assert (await world.review_store.get_state("acc")).current_question_id == "q1"


async def test_the_kit_store_refuses_what_it_cannot_do_instead_of_answering_wrongly():
    store = RecordingReviewStore()
    with pytest.raises(NotImplementedError):
        await store.purge_overdue(NOW)
    store.memory.collection("c").docs["x"] = {"a": 1}
    with pytest.raises(NotImplementedError):
        await store.memory.collection("c").where(filter=FieldFilter("a", ">", 1)).get()


# ---------------------------------------------------------------------------------------------- the other stand-ins

async def test_the_lease_holds_one_owner_per_account_until_it_expires_or_is_released():
    lease = MemoryLease()
    until = NOW + timedelta(minutes=30)

    assert await lease.acquire("acc", "a", NOW, until) is True
    assert await lease.acquire("acc", "a", NOW + timedelta(minutes=1), until) is False      # not even the same owner
    assert await lease.acquire("acc2", "b", NOW, until) is True
    await lease.release("acc", "other")                                                       # not its holder
    assert await lease.acquire("acc", "b", NOW + timedelta(minutes=2), until) is False
    await lease.release("acc", "a")
    assert await lease.acquire("acc", "b", NOW + timedelta(minutes=2), until) is True
    assert await lease.acquire("acc", "c", until, until + timedelta(minutes=5)) is True      # expired at `until`
    with pytest.raises(ValueError):
        await lease.acquire("", "a", NOW, until)
    with pytest.raises(ValueError):
        await lease.acquire("acc", "a", NOW, NOW)


async def test_the_task_queue_records_worker_tasks_once_per_name_and_refuses_everything_else():
    queue = RecordingTaskQueue()

    first = await queue.enqueue_worker_task("fact_review", {"mode": "next"}, dedup_key="k1")
    again = await queue.enqueue_worker_task("fact_review", {"mode": "next"}, dedup_key="k1")
    other = await queue.enqueue_worker_task("fact_review", {"mode": "daily"}, delay_seconds=120, deadline_seconds=1800,
                                            dedup_key="k2")

    assert first and other and again is None
    assert [(t["payload"]["mode"], t["delay_seconds"], t["dedup_key"]) for t in queue.tasks] == [
        ("next", 0, "k1"), ("daily", 120, "k2")]
    for method in ("enqueue_agent_task", "enqueue_consolidation_task", "enqueue_slack_event", "purge_queue"):
        with pytest.raises(LiveEffectRefused):
            getattr(queue, method)
    assert len(queue.tasks) == 2


async def test_appended_pairs_reach_the_runs_own_reads_and_never_the_live_session():
    world = ReviewWorld()
    sid = "usr-1:D1"
    await world.session_store.append_message(sid, Message(role="user", parts=[MessagePart(text="hello")]), owner_id=USER)
    before = live_state(world)
    sessions = NoAppendSessions(world.session_store)
    pair = [Message(role="user", parts=[MessagePart(text="q")]), Message(role="model", parts=[MessagePart(text="a")])]

    await sessions.append_messages_batch(sid, pair, owner_id=USER, raise_on_error=True)
    await sessions.append_message(sid, Message(role="user", parts=[MessagePart(text="3")]))

    assert [m.parts[0].text for m in (await sessions.load_session(sid)).history] == ["hello", "q", "a", "3"]
    assert len((await world.session_store.load_session(sid)).history) == 1
    assert live_state(world) == before
    assert [(s, len(b)) for s, b in sessions.appended] == [(sid, 2), (sid, 1)]
    with pytest.raises(LiveEffectRefused):
        await sessions.save_session(sid, await sessions.load_session(sid))
    assert (await NoAppendSessions().load_session("x:y")).history == []


async def test_the_platform_stand_in_numbers_its_posts_and_shows_a_choice_even_for_a_message_it_never_posted():
    platform = RecordingInteractive()
    world = ReviewWorld()
    fact = await stale_fact(world)
    question = await question_on(world, fact)
    from src.domain.fact_review_language import LABEL_KEYS, fact_question_message
    message = fact_question_message(question, {key: key for key in LABEL_KEYS})

    first = await platform.post("slack", "D1", message)
    second = await platform.post("slack", "D1", message)
    third = await platform.post("telegram", "D1", message)
    live_ref = MessageRef(platform="slack", channel_id="D1", message_id="1729760000.000100")
    await platform.show_choice(live_ref, ChoiceOutcome(message=message, note="Noted"))

    assert (first.message_id, second.message_id, third.message_id) == ("kit-1", "kit-2", "kit-1")
    assert [ref for ref, _ in platform.shown] == [live_ref]
    assert world.interactive.shown == {}                                   # the live platform was not touched


async def test_the_notifier_keeps_the_text_it_would_have_sent_and_sends_nothing(tmp_path):
    from scripts.consolidation.review_kit_stand_ins import KitNotificationState
    state = KitNotificationState(lambda: NOW)
    notifier = NoPostNotifier(state)

    assert await notifier.notify_raw(USER, ACC, "digest") is False         # no channel stored
    await state.save_primary(USER, "slack", "D1")
    assert await notifier.notify_raw(USER, ACC, "digest") is True
    assert await notifier.resolve_channel(USER) == await state.get_primary(USER)
    assert notifier.texts == [{"platform": "slack", "channel_id": "D1", "text": "digest"}]
    assert notifier._channel_factory.create("slack", "D1") is None


async def test_the_language_channel_and_agent_fakes_hold_what_they_are_given():
    from scripts.consolidation.review_kit_stand_ins import KitChannelBindings
    from src.domain.channel_binding import ChannelBinding
    language = KitLanguagePreferences()

    assert await language.get_preference(USER) == (None, True)
    assert await language.resolve_ui_language(USER) is LanguageCode.EN
    language.preferences[USER] = (LanguageCode.UK, False)
    assert await language.get_preference(USER) == (LanguageCode.UK, False)
    assert await language.resolve_ui_language(USER) is LanguageCode.UK
    bindings = KitChannelBindings()
    assert await bindings.get("D1") is None
    await bindings.save(ChannelBinding(channel_id="D1", agent_type="tutor", intent="tutor_chat", created_by=USER))
    assert (await bindings.get("D1")).agent_type == "tutor"
    await bindings.delete("D1")
    assert await bindings.get("D1") is None
    factory = NoopAgentFactory()
    await factory.ensure_agents_for_user(USER)
    assert factory.ensured == [USER]


# ---------------------------------------------------------------------------------------------- stage_stale

async def test_stage_stale_stales_in_the_overlay_with_the_janitors_fields_and_leaves_the_live_store_byte_identical(tmp_path):
    world = ReviewWorld()
    plain = await world.seed("walks daily")
    dated = await world.seed("flight booked", expires_at=NOW + timedelta(days=9))
    before = live_state(world)
    kit = guarded_kit(world, tmp_path)

    staged = await kit.stage_stale([plain, dated, plain], at=NOW)

    assert staged == [plain, dated]
    assert live_state(world) == before
    assert [f.id for f in await kit.repo.get_stale_facts(ACC)] == sorted([plain, dated], key=lambda i: i)
    fact = kit.repo.overlay_document(plain)
    assert (fact.state, fact.is_current, fact.stale_since, fact.last_updated) == (FactState.STALE, True, NOW, NOW)
    assert fact.lifecycle_events[-1]["actor"] == "janitor"
    assert kit.repo.overlay_document(dated).expires_at == NOW + timedelta(days=9)         # the fact's own date is kept
    assert [w["kind"] for w in kit.repo.writes] == ["stale_due_fact", "stale_due_fact"]


async def test_stage_stale_stages_all_or_none_and_refuses_a_fact_that_is_not_current(tmp_path):
    world = ReviewWorld()
    current = await world.seed("walks daily")
    already = await stale_fact(world)
    kit = guarded_kit(world, tmp_path)

    with pytest.raises(ValueError, match="not current"):
        await kit.stage_stale([current, already], at=NOW)
    with pytest.raises(ValueError, match="no document"):
        await kit.stage_stale(["nope"], at=NOW)

    assert kit.repo.writes == [] and kit.repo.overlay_document(current) is None


# ---------------------------------------------------------------------------------------------- an owner's answer

async def test_an_owner_answer_through_the_kit_closes_and_releases_in_memory_and_writes_nothing_live(tmp_path):
    world = ReviewWorld()
    fact = await stale_fact(world)
    await question_on(world, fact, question_id="q1")
    await world.seed_question(question_id="q2", fact_id="f-next", queue_key=NOW - timedelta(days=5))
    await world.seed_state(current_question_id="q1", sent=1, sent_on=NOW.date(), account_id=ACC)
    live_question_state = (await world.review_store.get_questions(["q1"]))[0]
    kit = guarded_kit(world, tmp_path)
    stand_ins = kit.review_stand_ins(account_repo=Accounts(), user_repo=Users(), clock=lambda: NOW)
    await stand_ins.store.load_account(world.review_store, ACC)
    before = live_state(world)
    posted_before = list(world.interactive.posted)

    result = await stand_ins.answer_service.apply(ACC, USER, "q1", AnswerKind.STILL_TRUE, "digit", NOW)

    assert result is AnswerResult.APPLIED
    assert live_state(world) == before and world.interactive.posted == posted_before
    assert world.interactive.shown == {} and world.enqueued == []
    assert (await world.review_store.get_questions(["q1"]))[0] == live_question_state     # still asked, live
    closed = next(q for q in stand_ins.store.questions() if q.question_id == "q1")
    assert (closed.status, closed.closed_by, closed.answer) == (QuestionStatus.CLOSED, ClosedBy.OWNER, AnswerKind.STILL_TRUE)
    assert stand_ins.store.state(ACC).current_question_id is None
    revived = kit.repo.overlay_document(fact)
    assert revived.state is FactState.CURRENT and revived.stale_since is None
    assert revived.lifecycle_events[-1]["reason"] == answer_reason(AnswerKind.STILL_TRUE, "q1", "digit")
    assert [t["payload"]["mode"] for t in stand_ins.task_queue.tasks] == ["next"]            # the release, recorded not enqueued
    assert len(stand_ins.interactive.shown) == 1
    assert [(r["action"], r["actor"], r["status"], r["cleanup"]) for r in kit.rows] == [("update", "owner", "updated", False)]
    report = kit.report()["review"]
    assert [q["status"] for q in report["questions"] if q["question_id"] == "q1"] == ["closed"]
    assert report["tasks"] == stand_ins.task_queue.tasks and report["session_appends"] == []
    assert {w["collection"].rsplit("_", 1)[-1] for w in report["store_writes"]} == {"questions", "state"}


async def test_the_answer_service_on_the_kit_applies_the_same_rules_as_production_to_every_answer(tmp_path):
    # The same answer on the live world and on the kit leaves the fact in the same state (the kit is the real service).
    states = {}
    for where in ("live", "kit"):
        world = ReviewWorld()
        fact = await stale_fact(world)
        await question_on(world, fact, question_id="q1")
        if where == "live":
            from tests.unit.services.test_fact_answer_service import service
            await service(world).apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", NOW)
            states[where] = world.docs[fact]["state"]
        else:
            kit = guarded_kit(world, tmp_path)
            si = kit.review_stand_ins(account_repo=Accounts(), user_repo=Users(), clock=lambda: NOW)
            await si.store.load_account(world.review_store, ACC)
            await si.answer_service.apply(ACC, USER, "q1", AnswerKind.NEVER, "digit", NOW)
            states[where] = kit.repo.overlay_document(fact).state.value
    assert states == {"live": "invalidated", "kit": "invalidated"}


# ---------------------------------------------------------------------------------------------- the review's report rows

async def test_the_reviews_rows_are_not_cleanup_rows_and_its_history_fact_is_not_counted_as_an_undated_create(tmp_path):
    world = ReviewWorld()
    fact = await stale_fact(world, "goes to the FitLife gym")
    kit = guarded_kit(world, tmp_path)
    ended = answer_reason(AnswerKind.ENDED, "q7", "digit")

    result = rendered(await kit.recorder.apply_fact_update(
        fact, parse_fact_update({"state": "archived"}), ACC, context=ctx("review"), evidence_at=NOW, reason=ended,
        history="No longer true (the owner said so in Oct 2026): goes to the FitLife gym",
    ))

    assert result["status"] == "updated"
    (row,) = kit.rows
    assert (row["action"], row["actor"], row["cleanup"]) == ("update", "review", False)
    report = kit.report()
    assert report["cleanup_creates_without_derived_from"] == {"per_pass": {}, "total": 0}
    assert report["store_writes"] == {"archive_and_add": 1}
    kit.close()
