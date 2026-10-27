"""What the two memory-review benches share and the review bench runs (Facts S4 plan T12b, §8 benches 6 and 7).

The review bench runs the real `FactReviewService.run` on the dry-run kit: the owner's live reads (primary channel,
language, stored sessions, the live stale facts and open questions) in, nothing out. Everything it stages (the seeded
cases, the facts it stales) lives in the kit's overlay and memory stores, one fresh set per run. Nothing here builds a
client or reads `.env`; the callers pass the ports they built.

What a bench prints is counts and verdicts; the report in `scripts/memory/` holds the fact text and the questions as the
owner would read them (gate G6).
"""
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AbstractSet, Callable, Dict, List, Mapping, Optional, Sequence

from scripts.consolidation.review_bench_cases import (
    FactCase,
    fact_entity,
    reply_cases,
    stale_cases,
    stale_moment,
)
from scripts.consolidation.review_kit_stand_ins import ReviewStandIns, build_review_service
from scripts.consolidation.s3_dry_run_kit import DryRunKit
from src.domain.entities import FactEntity, FactState
from src.domain.fact_lifecycle import expiry_pinned
from src.domain.fact_review_ask import AskRequest
from src.domain.fact_review_frame import question_text
from src.domain.fact_review_outcome import ReviewDecision
from src.domain.fact_review_result import FactReviewResult, decode_review_result
from src.domain.fact_review_reasons import (
    history_reason, is_revive_reason, is_rewrite_reason, queued_reason,
)
from src.domain.fact_review_rules import REVIEW_BATCH, QUEUE_ACCEPTS, queued_fields, queued_question
from src.domain.fact_review_state import FactReviewState
from src.domain.fact_write_context import as_utc
from src.domain.language import LanguageCode
from src.domain.pass_actor import PassActor
from src.domain.messaging import MessageRef, session_id_for
from src.infrastructure.agent_manifest import Intent

# What the gates read.
REVIEW_TASK = Intent.REVIEW_STALE_FACTS     # the payload task the service routes
REVIEW_PASS = PassActor.REVIEW.value        # the pass label in the kit's report (the actor of the pass)
G5_MAX_TURNS = 12                          # of the review's 15

# Plan §8's two reference points (a batch of 2 facts, a batch of 20), at the upper figure of each range, as dollars: the
# review's cost is near-linear in the batch. Sonnet / Opus, per run.
_COST_PER_RUN = {"sonnet": (0.0667, 0.01667), "opus": (0.1333, 0.03333)}


def utc_now() -> datetime:
    """The bench's one clock: the review service, the answers and `stage_stale(at=)` all read this."""
    return datetime.now(timezone.utc)


def estimate_usd(facts: int, runs: int) -> Dict[str, float]:
    """Plan §8's estimate for `runs` passes over `facts` facts, per model family."""
    return {name: round(runs * (base + slope * facts), 2) for name, (base, slope) in _COST_PER_RUN.items()}


def cost_banner(title: str, estimate: str) -> List[str]:
    """The lines every bench prints first: it spends money and writes nothing live."""
    return [
        "=" * 98,
        title,
        f"THIS BENCH MAKES PAID MODEL CALLS (estimate {estimate}); the cap is the kit's budget.",
        "It writes NOTHING live: facts, questions, counters, sessions, chat posts and tasks all stay in the kit's memory.",
        "The report (fact text included) goes to scripts/memory/ only; the terminal shows counts.",
        "=" * 98,
    ]


# ------------------------------------------------------------------------------------------------ staging

def live_staged_count(requested: int, legacy: int, fixed: int) -> int:
    """How many live facts to stage so the batch stays one batch: `requested`, but not past what `REVIEW_BATCH` leaves
    after the live stale facts (`legacy`) and the seeded cases (`fixed`). The review puts at most one batch before the
    model; a case that falls outside it would be a case never run."""
    return max(0, min(requested, REVIEW_BATCH - legacy - fixed))


def pick_staged(current: Sequence[FactEntity], count: int, skip: AbstractSet[str] = frozenset()) -> List[FactEntity]:
    """The `count` current facts to stage stale: those with an expiry (the janitor stales nothing else), the ones due
    soonest first; never a pinned fact (a directive, principle or mindset fact: its lifecycle is curated, not reviewed) and never a fact in `skip`."""
    eligible = [
        f for f in current
        if f.expires_at is not None and f.id not in skip and not expiry_pinned(f.domain, f.type, f.tags)
    ]
    eligible.sort(key=lambda f: (as_utc(f.expires_at), f.id))   # type: ignore[arg-type]  # filtered above
    return eligible[:max(0, count)]


@dataclass(frozen=True)
class Staged:
    """What a run's staging put in place."""

    legacy: int                  # live facts already stale
    live: List[str]              # live facts staged by the bench
    cases: Dict[str, str]        # case key -> fact id
    questions: Dict[str, str]    # case key -> question id (the reply cases)
    listed: Dict[str, str] = field(default_factory=dict)   # `--stage-ids`: fact id -> its state before it was staged stale
    listed_texts: Dict[str, str] = field(default_factory=dict)   # ... and its text as staged

    @property
    def batch(self) -> int:
        return min(REVIEW_BATCH, self.legacy + len(self.live) + len(self.cases) + len(self.listed))


async def seed_owner(
    bundle: ReviewStandIns, *, user_id: str, user_repo: Any, live_state: Any, mirror: Optional[str] = None,
    fixed: Optional[LanguageCode] = None,
) -> Dict[str, Any]:
    """The owner's primary channel (a read of the live notification state) and language setting (a read of his profile)
    into the kit's stand-ins. Unseeded, a run has no channel (nothing is sent) and mirror is on. `mirror` forces mirror
    mode on with no fixed language: the language the model reports is then read from the owner's own recent messages.
    `fixed` forces mirror mode off with that language: the pass then translates into it. Both given is an error."""
    if mirror and fixed is not None:
        raise ValueError("mirror and fixed cannot both be forced")
    channel = await live_state.get_primary(user_id)
    if channel is not None:
        await bundle.notification_state.save_primary(user_id, channel.platform, channel.channel_id)
        await bundle.notification_state.save(user_id, channel.platform, channel.channel_id)
    profile = await user_repo.get_user(user_id)
    preferred = profile.config.preferred_language if profile is not None else None
    if fixed is not None:
        follows, language = False, fixed
    else:
        follows = True if mirror else (profile.config.agent_mirror if profile is not None else True)
        language = None if mirror else preferred
    bundle.language.preferences[user_id] = (language, follows)
    return {
        "channel": None if channel is None else {"platform": channel.platform},
        "mirror": follows,
        "fixed_language": language.value if language is not None else None,
        "mirror_expected": mirror,
    }


async def _ask_about(
    kit: DryRunKit, bundle: ReviewStandIns, case: FactCase, fact_id: str, user_id: str, account_id: str,
    now: datetime, channel: str,
) -> str:
    """Queue the case's fact the review's way (made current under its precondition, a `queued` record), then the question
    goes out, is asked and the owner's words arrive: the question stored with its reply, unread."""
    stored = await kit.repo.resolve_current(fact_id, account_id, QUEUE_ACCEPTS)
    if stored is None:
        raise ValueError(f"the case {case.key} is not stale: it cannot be queued")
    question_id = uuid.uuid5(uuid.NAMESPACE_URL, f"question/{case.fact_id}").hex
    queued_at = now - timedelta(days=4)
    await kit.repo.update_fact_fields(stored, queued_fields(stored.fact, question_id, queued_at))
    question = queued_question(stored.fact, question_id, user_id, "revive", at=queued_at)
    asked_at = now - timedelta(days=3)
    ref = MessageRef(platform="kit", channel_id=channel, message_id=f"kit-seed-{case.key}")
    question = (
        question.claimed(session_id_for(user_id, channel)).appended(asked_at - timedelta(minutes=1)).posted(ref).asked(asked_at)
        .with_reply(case.reply or "", now - timedelta(days=1))
    )
    bundle.store.seed_question(question)
    return question_id


async def stage_listed(
    kit: DryRunKit, ids: Sequence[str], plan: Optional[Mapping[str, Mapping[str, Any]]], now: datetime,
) -> Dict[str, str]:
    """`--stage-ids`: each fact in `ids`, in ANY state, becomes a current copy in the kit's overlay (the live store is
    untouched) with the plan's expiry fields when it has an entry, then is staled with the janitor's fields at `now`.
    Returns id -> the state it had. A fact with no document raises ValueError before anything is staged."""
    originals: Dict[str, FactEntity] = {}
    for fact_id in dict.fromkeys(ids):
        stored = await kit.repo.get_fact_by_id(fact_id)
        if stored is None:
            raise ValueError(f"fact {fact_id} has no document: it cannot be staged")
        originals[fact_id] = stored
    for fact_id, stored in originals.items():
        update: Dict[str, Any] = {
            "state": FactState.CURRENT, "is_current": True, "valid_to": None, "stale_since": None, "superseded_by": None,
        }
        update.update((plan or {}).get(fact_id, {}))
        kit.repo.seed_document(FactEntity.model_validate({**stored.model_dump(), **update}))
    await kit.stage_stale(list(originals), at=now)
    return {fact_id: stored.state.value for fact_id, stored in originals.items()}


def _chosen_outcome(doc: Optional[FactEntity], asked: Optional[str]) -> str:
    """What the review chose for a listed fact, from its final overlay document and the question asked about it."""
    if asked is not None:
        return f"ask:{asked}"
    if doc is None or doc.state is FactState.STALE:
        return "left stale"
    if doc.state is not FactState.CURRENT:
        return {"archived": "archive", "invalidated": "invalidate", "superseded": "supersede"}[doc.state.value]
    reason = (doc.lifecycle_events[-1].get("reason") if doc.lifecycle_events else None)
    if is_rewrite_reason(reason):
        return "history" if str(reason).startswith("rewritten as history") else "rewrite"
    return "revive" if is_revive_reason(reason) else "current"


DEFAULT_MAX_PASSES = 6


def _pass_results(service: Any, start: int) -> List[FactReviewResult]:
    """The results of the review passes the service routed so far (from `start`), as the agent returned them."""
    return [
        decode_review_result(response.result) for response in service._coordinator.responses[start:]
        if getattr(response, "result", None) is not None
    ]


def undecided(kit: DryRunKit, listed: Mapping[str, str], results: Sequence[FactReviewResult]) -> List[str]:
    """The listed facts the review has not decided yet: still stale in the overlay and neither asked nor folded into an ask."""
    taken = {fold for result in results for ask in result.asks for fold in (ask.fact_id, *ask.folds)}
    return [
        fact_id for fact_id in listed
        if fact_id not in taken and (doc := kit.repo.overlay_document(fact_id)) is not None and doc.state is FactState.STALE
    ]


def _what_it_writes(
    kit: DryRunKit, doc: Optional[FactEntity], decision: Optional[ReviewDecision], ask: Optional[AskRequest],
    stored: Optional[Any], folded_into: Optional[str],
) -> Dict[str, Any]:
    """What the review would write for one listed fact: the new text of a rewrite or history (where happened / did not
    happen / unknown is visible), an ask's kind with every pre-written text, the fallback and the folds, a supersede's
    successor, an archive's or invalidate's basis."""
    if folded_into is not None:
        return {"folded_into": folded_into}
    if ask is not None or stored is not None:   # no ask: a question code queued for a fact left undecided twice
        question: Dict[str, Any] = {
            "question": ask.question if ask is not None else stored.question,
            "texts": dict(ask.texts) if ask is not None else dict(stored.texts),
            "fallback": ask.fallback if ask is not None else stored.fallback,
            "fallback_date": ask.fallback_date.isoformat() if ask is not None and ask.fallback_date is not None else None,
            "noise": ask.noise if ask is not None else stored.noise, "folds": list(ask.folds) if ask is not None else [],
        }
        if stored is not None:
            question.update({"delivery": stored.delivery, "origin": stored.origin, "stored_texts": dict(stored.texts)})
        return question
    if decision is not None and decision.outcome in ("rewritten", "revived"):
        written = kit.repo.overlay_document(decision.written_id)
        return {
            "new_text": written.text if written is not None else None, "written_id": decision.written_id,
            "as_history": decision.as_history, "basis": decision.basis_kind,
        }
    if doc is None:
        return {}
    if doc.state is FactState.SUPERSEDED:
        successor = kit.repo.overlay_document(doc.superseded_by) if doc.superseded_by else None
        return {"successor_id": doc.superseded_by, "successor_text": successor.text if successor is not None else None}
    if doc.state in (FactState.ARCHIVED, FactState.INVALIDATED):
        return {"basis": decision.basis_kind if decision is not None else None}
    return {}


def listed_report(
    kit: DryRunKit, bundle: ReviewStandIns, listed: Mapping[str, str], texts: Optional[Mapping[str, str]] = None,
    results: Sequence[FactReviewResult] = (),
) -> List[Dict[str, Any]]:
    """Per listed fact: id, state before staging, original text, the outcome the review chose, WHAT it would write (the
    new text, the question with its texts, fallback and folds, the successor, the basis) and the code-built reason line
    of the fact's trail."""
    questions = {q.fact_id: q for q in bundle.store.questions()}
    decisions = {d.fact_id: d for result in results for d in result.outcomes}
    asks = {a.fact_id: a for result in results for a in result.asks}
    folded = {fold: ask.fact_id for result in results for ask in result.asks for fold in ask.folds}
    rows: List[Dict[str, Any]] = []
    for fact_id, original in listed.items():
        doc = kit.repo.overlay_document(fact_id)
        asked = questions.get(fact_id)
        last = doc.lifecycle_events[-1] if doc is not None and doc.lifecycle_events else {}
        decision = decisions.get(fact_id)
        if fact_id in folded:
            outcome = "folded"
        elif asked is None and decision is not None and decision.outcome in ("rewritten", "revived"):
            outcome = "revive" if decision.outcome == "revived" else ("history" if decision.as_history else "rewrite")
        else:
            outcome = _chosen_outcome(doc, asked.question if asked is not None else None)
        rows.append({
            "fact_id": fact_id, "original_state": original,
            "original_text": (texts or {}).get(fact_id, doc.text if doc is not None else None),
            "text": doc.text if doc is not None else None,
            "outcome": outcome, "final_state": doc.state.value if doc is not None else None,
            "writes": _what_it_writes(kit, doc, decision, asks.get(fact_id), asked, folded.get(fact_id)),
            "actor": last.get("actor"), "reason": last.get("reason"),
        })
    return rows


def listed_markdown(runs: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The human file of a `--stage-ids` run: one block per fact, per run. None when no run listed facts."""
    runs = [run for run in runs if "listed" in run]
    if not runs:
        return None
    lines = ["# Listed facts: what the review would write", ""]
    for number, run in enumerate(runs, 1):
        left = run.get("left_undecided", [])
        lines += [f"## Run {number}: {run.get('passes_run', 1)} pass(es), {len(left)} fact(s) left undecided", ""]
        if left:
            lines += ["Left undecided: " + ", ".join(left), ""]
        for row in run["listed"]:
            lines += [f"### {row['fact_id']}", "", f"- Original state: {row['original_state']}",
                      f"- Original text: {row['original_text']}", f"- Outcome: {row['outcome']}"]
            writes = row.get("writes") or {}
            if "new_text" in writes:
                lines.append(f"- Text it would write: {writes['new_text']}")
            if "question" in writes:
                lines.append(f"- Question: {writes['question']} (fallback {writes['fallback']}"
                             + (", noise" if writes.get("noise") else "") + ")")
                lines += [f"  - {key}: {text}" for key, text in writes["texts"].items()]
                if writes["folds"]:
                    lines.append("  - folds: " + ", ".join(writes["folds"]))
            for key in ("successor_id", "successor_text", "folded_into", "basis"):
                if writes.get(key) is not None:
                    lines.append(f"- {key.replace('_', ' ').capitalize()}: {writes[key]}")
            lines += [f"- Reason: {row['reason']}", ""]
    return "\n".join(lines)


async def stage_inputs(
    kit: DryRunKit, bundle: ReviewStandIns, *, user_id: str, account_id: str, requested: int, now: datetime,
    channel: Optional[str], listed_ids: Optional[Sequence[str]] = None,
    expiry_plan: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Staged:
    """The run's input: the live stale facts as they are, the seeded cases (documents in the overlay, staled with the
    janitor's fields at the moment each went stale; the reply cases also asked and answered), and `requested` live
    facts staled at `now`, the review's clock. Nothing is written live.

    With `listed_ids` (`--stage-ids`) the input is exactly those facts (any state, with the plan's expiry): no seeded
    case and no other live fact is staged; the live stale facts still come first by age."""
    legacy = len(await kit.repo.get_stale_facts(account_id))
    if listed_ids is not None:
        states = await stage_listed(kit, listed_ids, expiry_plan, now)
        texts = {fact_id: kit.repo.overlay_document(fact_id).text for fact_id in states}   # type: ignore[union-attr]
        return Staged(legacy, [], {}, {}, states, texts)
    stale, replies = stale_cases(now), reply_cases(now)
    cases: Dict[str, str] = {}
    questions: Dict[str, str] = {}
    for case in (*stale, *replies):
        kit.repo.seed_document(fact_entity(case, account_id, user_id))
        cases[case.key] = case.fact_id
        await kit.stage_stale([case.fact_id], at=stale_moment(case, now))
    for case in replies:
        questions[case.key] = await _ask_about(kit, bundle, case, case.fact_id, user_id, account_id, now, channel or "D-kit")
    live = pick_staged(
        await kit.repo.get_active_facts(account_id), live_staged_count(requested, legacy, len(cases)), set(cases.values()),
    )
    staged_live = await kit.stage_stale([f.id for f in live], at=now)
    return Staged(legacy, staged_live, cases, questions)


# ------------------------------------------------------------------------------------------------ one run

def unbased_revives(events: Sequence[Dict[str, Any]]) -> List[str]:
    """The reasons of the review's moves of a fact back to current that name no basis: a revive on evidence or on the
    owner's reply is `revived on …`; a rewrite, a queueing (the fact waits for its question) and a history fact born
    beside an archive (`history of <id>`) are no revives."""
    return [
        str(e.get("reason")) for e in events
        if e.get("actor") == REVIEW_PASS and e.get("to") == "current"
        and not (is_revive_reason(e.get("reason")) or is_rewrite_reason(e.get("reason"))
                 or str(e.get("reason", "")).startswith((queued_reason(""), history_reason(""))))
    ]


async def run_outcomes(kit: DryRunKit, case_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    """Every fact the run changed, as the owner reads the table (G6): case key (or `live`), final state, the last trail
    entry, the text."""
    by_id = {fact_id: key for key, fact_id in case_ids.items()}
    seen: Dict[str, None] = {}
    for write in kit.repo.writes:
        if write["kind"] == "update_fact_fields":
            seen.setdefault(write["fact_id"])
        elif write["kind"] in ("write_superseding", "archive_and_add"):
            for fact_id in (*write.get("replaced", []), write.get("archived_id"), write.get("new_id")):
                if fact_id:
                    seen.setdefault(fact_id)
    rows: List[Dict[str, Any]] = []
    for fact_id in seen:
        doc = kit.repo.overlay_document(fact_id)
        if doc is None:
            continue
        last = doc.lifecycle_events[-1] if doc.lifecycle_events else {}
        rows.append({
            "case": by_id.get(fact_id, "live"), "fact_id": fact_id, "state": doc.state.value,
            "actor": last.get("actor"), "reason": last.get("reason"), "text": doc.text,
            "unbased_revives": unbased_revives(doc.lifecycle_events),
        })
    return rows


async def run_once(
    kit: DryRunKit,
    handles: Any,
    live_state: Any,
    *,
    user_id: str,
    account_id: str,
    requested: int,
    mirror: Optional[str],
    fixed: Optional[LanguageCode] = None,
    clock: Callable[[], datetime] = utc_now,
    listed_ids: Optional[Sequence[str]] = None,
    expiry_plan: Optional[Mapping[str, Mapping[str, Any]]] = None,
    max_passes: int = DEFAULT_MAX_PASSES,
) -> Dict[str, Any]:
    """One review run on the kit from the live state: a fresh overlay, fresh stand-ins, the live open questions and review
    state loaded, the owner's channel and language seeded, the input staged, then `FactReviewService.run` (daily)."""
    kit.new_run()
    bundle = kit.review_stand_ins(
        account_repo=handles.account_repo, user_repo=handles.user_repo, live_sessions=handles.container.session_store,
        clock=clock,
    )
    await bundle.store.load_account(handles.container.fact_review_store, account_id)
    if bundle.store.state(account_id) is None:
        bundle.store.seed_state(FactReviewState(account_id=account_id, owner_user_id=user_id))
    owner = await seed_owner(bundle, user_id=user_id, user_repo=handles.user_repo, live_state=live_state, mirror=mirror, fixed=fixed)
    channel = await bundle.notification_state.get_primary(user_id)
    now = bundle.clock()
    staged = await stage_inputs(
        kit, bundle, user_id=user_id, account_id=account_id, requested=requested, now=now,
        channel=channel.channel_id if channel is not None else None, listed_ids=listed_ids, expiry_plan=expiry_plan,
    )
    service = build_review_service(
        kit, bundle, agent=handles.agent, account_repo=handles.account_repo, user_repo=handles.user_repo,
    )
    first_row, first_pass, first_post = len(kit.rows), len(kit.passes), len(kit.sink.posts)
    result = await service.run(user_id, account_id, "daily")
    passes_run = 1
    results = _pass_results(service, 0) if listed_ids is not None else []
    while listed_ids is not None and result.get("status") == "completed" and passes_run < max_passes:
        if not undecided(kit, staged.listed, results):
            break
        result = await service.run(user_id, account_id, "daily")   # the next batch over the overlay, which keeps the results
        passes_run += 1
        results = _pass_results(service, 0)
    outcomes = await run_outcomes(kit, staged.cases)
    state = bundle.store.state(account_id)
    left = undecided(kit, staged.listed, results) if listed_ids is not None else []
    listed_rows = {
        "listed": listed_report(kit, bundle, staged.listed, staged.listed_texts, results),
        "passes_run": passes_run, "max_passes": max_passes, "left_undecided": left,
    } if listed_ids is not None else {}
    return {
        **listed_rows,
        "result": result,
        "owner": owner,
        "batch": staged.batch,
        "legacy": staged.legacy,
        "live_staged": len(staged.live),
        "cases": staged.cases,
        "rows": [first_row, len(kit.rows)],
        "passes": [first_pass, len(kit.passes)],
        "alerts": kit.sink.posts[first_post:],
        "outcomes": outcomes,
        "unbased_revives": [r for row in outcomes for r in row["unbased_revives"]],
        "message_language": state.message_language if state is not None else None,
        "label_languages": sorted(state.labels) if state is not None else [],
        "translations": [
            {"language": d["language"], "source_digest": d["source_digest"], "shown": d["text"]}
            for d in bundle.store.quote_translation_docs()
        ],
        "question_texts": [question_text(message) for _, message in bundle.interactive.posted],
        "stand_ins": bundle.report(),
    }


# ------------------------------------------------------------------------------------------------ gates and counts

def run_counts(run: Dict[str, Any], kit: DryRunKit) -> List[str]:
    """One run's counts, without a word of fact text."""
    rows = kit.rows[run["rows"][0]:run["rows"][1]]
    actions = Counter(f"{r['action']}:{r['status']}" for r in rows)
    questions = Counter(q["status"] for q in run["stand_ins"]["questions"])
    listed = Counter(row["outcome"] for row in run.get("listed", []))
    return [
        *(["listed facts: " + ", ".join(f"{k} {v}" for k, v in sorted(listed.items()))] if "listed" in run else []),
        *([f"review passes: {run['passes_run']} of at most {run['max_passes']}; left undecided: {len(run['left_undecided'])}"]
          if "left_undecided" in run else []),
        f"run {run['result'].get('status')}: batch {run['batch']} of {run['legacy']} stale + {run['live_staged']} staged "
        f"+ {len(run['cases'])} cases",
        "writes: " + (", ".join(f"{k} {v}" for k, v in sorted(actions.items())) or "none"),
        "questions after: " + (", ".join(f"{k} {v}" for k, v in sorted(questions.items())) or "none"),
        f"posts {len(run['stand_ins']['posts'])}, texts {len(run['stand_ins']['texts'])}, tasks "
        f"{len(run['stand_ins']['tasks'])}, alerts {len(run['alerts'])}",
    ]


def review_summary(report: Dict[str, Any], extra: Dict[str, Any]) -> List[str]:
    """The lines the owner reads against G5 and G6 (and the mirror run's language)."""
    passes = [p for p in report.get("passes", []) if p["pass"] == REVIEW_PASS]
    lines: List[str] = []
    if passes:
        worst = max(p["llm_calls"] for p in passes)
        lines.append(
            f"G5 {'FAIL' if worst > G5_MAX_TURNS else 'ok'}: {len(passes)} review pass(es), most model turns {worst} "
            f"(limit {G5_MAX_TURNS})"
        )
        abandoned = sum(p["abandoned_refusals"] for p in passes)
        lines.append(f"G5 {'FAIL' if abandoned else 'ok'}: {abandoned} abandoned refusal(s)")
        lines.append(f"G5 info: born-due refusals reported {sum(p['born_due'] for p in passes)}")
    else:
        lines.append("G5: no review pass ran (nothing selected, or the run failed before the model)")
    unbased = sum(len(run.get("unbased_revives", [])) for run in extra.get("runs", []))
    lines.append(f"G5 {'FAIL' if unbased else 'ok'}: {unbased} revive(s) without evidence or a reply")
    lines.append("G6: read the outcomes table, the question texts and the labels in the report")
    for number, run in enumerate(extra.get("runs", []), 1):
        expected = (run.get("owner") or {}).get("mirror_expected")
        if expected:
            reported = run.get("message_language")
            lines.append(
                f"mirror run {number}: reported language {reported!r} (expected {expected!r}), "
                f"label sets kept for {run.get('label_languages')}"
            )
    for number, run in enumerate(extra.get("runs", []), 1):
        kept = run.get("translations", [])
        languages = sorted({t["language"] for t in kept})
        lines.append(f"run {number}: {len(kept)} translation(s) kept ({', '.join(languages) or 'none'})")
    if report.get("spend", {}).get("aborted"):
        lines.append("RUN STOPPED by the spend meter (cap or unpriced model): the passes above are incomplete")
    return lines
