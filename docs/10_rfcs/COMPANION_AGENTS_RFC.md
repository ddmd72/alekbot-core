# RFC: Companion agents and session-scoped memory

**Status:** Proposed — text language tutor first
**Date:** 2026-08-17
**Owner:** Dmytro
**Milestone:** A second agent family

**Related:** `VOICE_COMPANION_RFC.md` (§4.5 "Lelik is not an agent" is reversed here, §6),
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
everything already stored.

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
short drill turns are not 70 conversational ones).

**Read:** whether the session may read the user's personal store at all, and which domains. The
mechanism exists — `enrich_context(relevant_domains=…)` — but today the Router picks domains per
request by classification. A companion declares them instead.

The read side is a permission boundary, not a preference: with two separate stores, "may this
session read the person's life" is a real edge. Default is no.

**Today's behaviour is the default policy.** No existing call site changes; only new companions
declare something else. The three ad-hoc flags are absorbed one at a time afterwards, each
migration proving the axis fits — rather than a system-wide refactor that proves it in advance.

## 6. Companions are ordinary agents

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

## 7. Scope: conversational channels only

The axis governs the memory policy of a **conversation**. Email indexing is not an instance of
it and must not be bent into one: its trigger is a scheduled job rather than a sliding window,
and its records have their own shape (`IndexedEmail`, its own collection) rather than a
different domain. Email is a different *source*, not a different conversation policy.

Stretching the axis to cover it would distort both.

## 8. Plan

1. **Text language tutor.** Exercises exactly the new axis — progress instead of biography,
   drills as noise — with no realtime code at all. If the model does not hold here it will not
   hold on voice.
2. **Call layer:** per-call sync/async, cycle guard.
3. **Absorb the ad-hoc flags** one at a time: `ChannelBinding` stateless first, since it is the
   one the tutor directly contradicts.
4. **Realtime transport** — its own RFC, once the Twilio-vs-own-UI fork is decided. It is a
   transport that can front any agent, and it must not be allowed to define the family.

## 9. Open questions

1. **Group-session threshold and account attribution** — participants share one account, so
   billing has an anchor, but which member's counters a group turn increments is undecided.
2. **Moderation** — a separate task, deliberately out of scope here. Technically unblocked (the
   bot receives every message with its sender), but a moderator reads people who are not
   account members, and what may be recorded about them is a consent question, not an
   architectural one.
3. **Where the tutor's records live** — its own collection, keyed by session. Whether they reuse
   the `FactEntity` shape or need their own is settled when the tutor's extractor is written.

## 10. Rejected

- **A parallel `CompanionAgent` base class.** Buys nothing — persona, tools, tier and provider
  are already per-agent configuration — and costs the whole ecosystem: billing scope, spans,
  registry, delegation.
- **One configurable pipeline for both families.** Rejected once the identity models diverged
  (§2). Before that it looked like the elegant answer; it stopped being one the moment sessions
  could have several participants.
- **A hand-maintained policy × transport compatibility matrix.** It rots on the first new
  transport. Instead a policy declares the capability it needs and a transport declares what it
  provides, so incompatibility is computed. Concrete case: "write every turn to history" needs a
  server-side transcript, which browser realtime cannot supply and telephony can.
