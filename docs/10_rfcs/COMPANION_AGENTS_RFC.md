# RFC: Companion agents and session-scoped memory

**Status:** Proposed — text language tutor first
**Date:** 2026-08-17
**Owner:** Dmytro
**Milestone:** A second agent family

**Related:** `VOICE_COMPANION_RFC.md` (§4.5 "Lelik is not an agent" is reversed here, §7),
`PLATFORM_SESSION_ISOLATION_RFC.md` (per-channel sessions), `STANDING_DIRECTIVES_RFC.md`
(a non-biographical fact domain as prior art)

---

## 1. Problem

Alek has one memory policy, hardcoded: every turn enters session history, a sliding window
fills, consolidation runs the "Life Chronicler" protocol, facts land in the user's store.

A companion — a language tutor first, other roles later — needs different answers to all three
questions. Small talk floods consolidation with worthless material. A tutor should remember
recurring errors and coverage, not the drills that produced them. What it writes belongs in
`EDUCATION`/`SKILL`, never in the biography it would otherwise pollute.

Today that variation is expressible only as three ad-hoc flags that do not compose:
`MessagePart.consolidation_text` (store something other than what was shown),
`notify(save_history=False)` (store nothing), and `ChannelBinding` being stateless (no session
writes, no consolidation). The last one is the trap: it conflates **"do not consolidate"** with
**"consolidate differently"**, and a tutor needs the second.

Read as one axis instead of three flags, all of them are answers to the same question — *where
does this content land: the user's long-term store, a session-scoped long-term store, the
short-term session buffer only, or nowhere?* — decided ad hoc at three different call sites
instead of once. §10.3 records this explicitly and defers unifying it.

## 2. Two memory subsystems, not one configurable pipeline

**Decision.** Companion memory is a second subsystem with its own identity model, not a
configuration of Alek's.

| | Alek | Companion |
|---|---|---|
| Keyed by | user | **session** |
| Holds | facts about a life | records about an interaction |
| Participants | one | one or several |

The identity models differ, and that is what makes them two things. Forcing one pipeline to
serve both would mean teaching the user-scoped store to be sometimes-session-scoped — the kind
of unification that reads clean in a diagram and fights you in every query.

Note what this shrinks: with two subsystems, the "memory policy" a channel declares reduces to
**which subsystem it writes to and with which extractor**. Not forty settings.

## 3. Session is a first-class entity

- **Id derives from the channel, not the user.** Today `session_id = f"{user_id}:{channel_id}"`
  — the user is *inside the key*, so a group conversation would produce one session per
  participant for a single dialogue. Companion sessions are keyed by channel, platform-qualified
  so the key stays self-describing next to Alek's `user:channel` keys.
- **Participants** are the account members who write in the channel. Membership needs no new
  structure: `UserProfile.account_id` already expresses it and team invites already move users
  into an account.
- **Multi-user stays within one account.** This is what keeps `account_id` the billing and
  tenancy anchor and keeps cross-tenant authorization from ever arising.
- **A user's history is the union of the sessions they participated in.** Cross-store questions
  ("how is my Spanish?") are answered by *calling the companion*, not by a shared table.

**Build the identity now; the group machinery later.** Records keyed by session with a
participant list of length one costs nothing today. Normally the rule is the opposite — let a
future need break something, because the break is the signal. An identity model is the one
known exception: retrofitting it does not break loudly, it silently demands a migration of
everything already stored. §6 applies the same exception to the record *schema*, not only to the
session key.

## 4. The binding is the policy

One companion per channel. A second binding is **refused**, not merged.

Not because merging is expensive — because it is meaningless. Two extractors over one window
produce two interpretations of the same conversation in two stores, neither wrong, with no rule
for reconciling them. A session cannot have two memory policies.

Nothing is lost: two channels are two sessions and two policies, and channels are free.

- Any account member present in the chat may bind — and may unbind. Asymmetry would leave a
  binding made by a departed member unclearable.
- A chat may contain people who are not users at all; they are not participants.
- **Consequence to state out loud:** a bound channel bypasses Router, so Alek is not directly
  reachable there. Inside a tutor's channel you reach Alek *through* the tutor, by delegation.

## 5. What a policy declares

**Write:** the extraction protocol; the destination it may write; the window threshold (100
short drill turns are not 70 conversational ones); how a model turn is serialized into the batch
(`consolidation_text`'s summary-vs-full choice, per channel rather than hardcoded).

**Read:** whether the session may read the user's personal store at all, and which domains. The
mechanism exists — `enrich_context(relevant_domains=…)` — but today the Router picks domains per
request by classification. A companion declares them instead, as a small toggle-set rather than
a fixed list (§6) — which mix is right is not yet known.

The read side is a permission boundary, not a preference: with two separate stores, "may this
session read the person's life" is a real edge. Default is no.

**Today's behaviour is the default policy.** No existing call site changes; only new companions
declare something else. The three ad-hoc flags are absorbed one at a time afterwards, each
migration proving the axis fits — rather than a system-wide refactor that proves it in advance.

See §6 for the concrete mechanism: schema, shared services, `ChannelBinding.companion_config`.

## 6. Session-scoped memory: shared infrastructure across the family

The tutor is the pilot; a group-chat moderator is already named as the next companion (§10.2,
still out of scope as a *feature*). That makes the record schema and repository shared
infrastructure to decide once, not a tutor-specific detail to generalize later — the same
exception §3 already made for the session key applies to the record shape: it is the one thing
that does not fail loudly when wrong, it demands a migration.

**Shared — one implementation for the whole family:**

- **`CompanionRecord`** (domain) — one document per record, not one document per session with an
  array (vector `find_nearest` needs per-document granularity, so this is `FactEntity`'s per-row
  shape, not `Session`'s one-doc-with-array shape): `id`, `session_id` (the operative retrieval
  key — this *is* the identity model, §2), `account_id` + `created_by_user_id` (billing anchor +
  attribution, carried the same way `SessionStore.append_messages_batch` already carries
  `owner_id` alongside `session_id` without it being part of the key), `text`, `vector`, `tags`,
  a `domain`/`type` field so a tutor's "recurring subjunctive error" and a moderator's
  "volunteered to bring snacks" are different *values*, not different schemas. No SCD2 fields
  (`lineage_id`/`valid_from`/`valid_to`) — session records accumulate, they do not supersede a
  prior truth the way a biography does.
- **`CompanionMemoryRepository`** (port) + Firestore adapter, one collection, filtered primarily
  by `session_id`. `account_id` is an indexed field on every record from day one — cheap now,
  expensive to backfill later — but a repository method that queries *by* `account_id` (cross-
  channel rollups, billing analytics) is added only once a concrete caller needs it. The field is
  schema, expensive to add after the fact; the method is behavior, cheap to add later.
- **RRF fusion** — `SearchEnrichmentService._apply_rrf_ranking` already operates on nothing but
  `fact_id`-shaped ranked lists, with no `FactEntity` or identity coupling in the algorithm
  itself. Extract it to a standalone `domain/` function once; both Alek's enrichment and the
  companion assembler below call the same code.
- **Embedding** — `EmbeddingService`/`GeminiEmbeddingAdapter` reused unchanged; already a generic
  `text -> vector` port with zero entity coupling.
- **A context-assembler service**, structurally parallel to `SearchEnrichmentService` but built
  over `CompanionMemoryRepository`: one implementation, because "cached summary + query-dependent
  RRF over this session's history" is the same operation whether the content is grammar drills or
  group decisions.
- **A per-session cache document** (`session_id -> summary`) as the biography-cache analog —
  without `BiographicalContextService`'s `AccountRepository` billing-config dependency, which is
  Alek-specific and has no session equivalent.

**Not shared — one per companion type:**

- **The extractor.** What from a raw session batch is worth keeping is a judgment call specific
  to the companion (tutor: errors and coverage; moderator: decisions and commitments) — the same
  architectural slot `ConsolidationAgent` fills for Alek. One prompt/agent per companion type, all
  writing through the one shared repository above.
- **`ChannelBinding.companion_config`** — per-channel values (window threshold, batch size,
  `text_mode: summary|full`, which read-context providers are enabled, §5) are configuration, not
  code; they vary per channel, not per companion type.

**Read side, concretely** — the toggle-set §5 defers to here:

```
include_biographical: bool
session_domains: list[FactDomain]       # narrower slice than "all biographical", if needed
include_standing_directives: bool
include_own_records: bool               # this companion's own CompanionRecord history
```

Exact `FactDomain` values to expose are not decided here — verify against the current enum when
the first companion's read config is written, not from this document.

## 7. Companions are ordinary agents

They inherit `BaseAgent`, declare intents in the manifest, and are reached through the registry
like any specialist. This **reverses `VOICE_COMPANION_RFC.md` §4.5**, which made the voice
persona a handler rather than an agent.

The reversal is forced by a requirement that RFC did not have: companions must call Alek and be
called by Alek, synchronously and asynchronously. Both mechanisms already exist and are
agent-agnostic — `coordinator.handle_delegation` and `enqueue_agent_task` — and both work in
either direction *provided both sides are registered agents*. Being "not an agent" is precisely
what would require a second dispatch mechanism.

What is genuinely missing in the call layer, and must ship before the first companion:

1. **Sync/async is a property of the intent, not of the call.** `mode = manifest.capabilities[intent]`
   — the caller cannot choose. Companions need both "answer me now" and "get back to me".
2. **There is no cycle or depth guard.** `calling_agent_id` is documented as *logging only*.
   Today's graph is acyclic by convention (orchestrator → specialist, specialists only call
   downward). A companion that calls Alek, who can call the companion, makes cycles reachable —
   and a prompt is all it takes.

Both shipped 2026-08-25: per-call `mode_override` on `handle_delegation`, and a `_call_chain`
cycle guard with `MAX_DELEGATION_DEPTH=8` (`decisions/delegation_cycle_guard.md`).

## 8. Scope: conversational channels only

The axis governs the memory policy of a **conversation**. Email indexing is not an instance of
it and must not be bent into one: its trigger is a scheduled job rather than a sliding window,
and its records have their own shape (`IndexedEmail`, its own collection) rather than a
different domain. Email is a different *source*, not a different conversation policy.

Stretching the axis to cover it would distort both.

## 9. Plan

1. **Text language tutor.** Exercises exactly the new axis — progress instead of biography,
   drills as noise — with no realtime code at all. Ships the shared infrastructure from §6, not
   a tutor-specific store: it is the first caller, not a special case. If the model does not hold
   here it will not hold on voice.
2. **Call layer:** per-call sync/async, cycle guard. Done (§7).
3. **Absorb the ad-hoc flags** one at a time: `ChannelBinding` stateless first, since it is the
   one the tutor directly contradicts.
4. **Realtime transport** — its own RFC, once the Twilio-vs-own-UI fork is decided. It is a
   transport that can front any agent, and it must not be allowed to define the family.

## 10. Open questions

1. **Group-session threshold and account attribution** — participants share one account, so
   billing has an anchor, but which member's counters a group turn increments is undecided.
2. **Moderation** — a separate task, deliberately out of scope here. Technically unblocked (the
   bot receives every message with its sender), but a moderator reads people who are not
   account members, and what may be recorded about them is a consent question, not an
   architectural one. Named in §6 only as the reason the memory infrastructure is built shared,
   not as a feature being scoped now.
3. **Unifying the three ad-hoc flags (§1) into one write-destination policy.** `consolidation_text`,
   `notify(save_history=)`, `ChannelBinding` statelessness, and the companion destination (§6) are
   four values of one concept — where a piece of content is written — expressed as three
   different mechanisms at three call sites instead of one. Not building the unification now:
   retrofitting three working paths onto a shared mechanism is a larger, riskier change than
   shipping the tutor, and orthogonal to it. Revisit when something needs to *touch* one of the
   three existing flags again — not merely when another companion ships.

## 11. Rejected

- **A parallel `CompanionAgent` base class.** Buys nothing — persona, tools, tier and provider
  are already per-agent configuration — and costs the whole ecosystem: billing scope, spans,
  registry, delegation.
- **One configurable pipeline for both families.** Rejected once the identity models diverged
  (§2). Before that it looked like the elegant answer; it stopped being one the moment sessions
  could have several participants.
- **Reusing `FactEntity`/`FactRepository` for companion records**, tagging `session_id` in via
  metadata and filtering at query time. `FactRepository`'s methods hardcode `account_id`/`user_id`
  as the resolution key and `RequestContext` assumes that shape; routing session identity through
  the same schema and collection blends two identity models into one store — the exact failure
  §2 already rejected at the pipeline level, only moved down into storage. §6 builds a separate
  port instead, sharing only the identity-agnostic pieces (RRF, embedding).
- **A hand-maintained policy × transport compatibility matrix.** It rots on the first new
  transport. Instead a policy declares the capability it needs and a transport declares what it
  provides, so incompatibility is computed. Concrete case: "write every turn to history" needs a
  server-side transcript, which browser realtime cannot supply and telephony can.
