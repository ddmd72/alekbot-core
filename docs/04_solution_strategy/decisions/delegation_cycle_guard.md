# Delegation cycle guard: a call chain, not a depth counter

**Date:** 2026-08-25
**Status:** Done

## Decision

`AgentCoordinator.handle_delegation` carries `context["_call_chain"]` — the agent ids already
entered in this delegation. Re-entering one is refused with the path named; a
`MAX_DELEGATION_DEPTH=8` cap runs alongside it. Refusal returns `AgentResponse.failure` **and**
posts to `AlertSinkPort`.

Closes the first gap named in `COMPANION_AGENTS_RFC.md` §6. Until now `calling_agent_id` was
documented as *"For logging only"* and the graph was acyclic purely by convention: orchestrators
called specialists, and the three specialists that delegate only called downward. Companions break
that convention by design — they call Alek, and Alek can call them back.

## Why both checks

They cover different failures and neither subsumes the other. The chain catches a true cycle and
can **name** it, which a counter cannot — `"too deep"` is not actionable, `tutor → smart → tutor`
is. The cap catches a runaway of all-**distinct** agents, which a cycle check by construction never
trips. 8 is far above the deepest legitimate path (Smart → notes → compute).

## Why an alert, not just a failure

By existing tech debt a specialist failure is wrapped as a tool result and the orchestrator still
returns SUCCESS. A refusal that only reaches the logs would leave the loop invisible — and through
the async path the loop is *already* invisible: every hop returns an ack immediately, so nothing
blocks, nothing times out, and it looks like ordinary activity while spending money. That is the
failure this guard exists for, and it is why the chain must survive the Cloud Task payload.

## Rejected

- **Depth counter alone** — cheap, but cannot name the loop and kills legitimate deep chains at
  whatever number is chosen.
- **Tracking in a ContextVar** — would not survive the async hop, which is the case that matters.
- **A registry-level static acyclicity check** — `AgentDescriptor.allowed_intents` already
  restricts who may call what, and it is the right place for prevention *by design*. It cannot see
  runtime intent remaps or fan-out, so it complements the chain rather than replacing it.
- **Passing `mode_override` only when set**, to spare a hand-written test stub that mirrored the
  coordinator signature — that lets a test dictate production shape, and the next rigid stub would
  break the same way.

## Notes

`context["_call_chain"]` is appended into a **new** dict, never in place: one caller fans a tool
batch out through `asyncio.gather`, and a shared list would let siblings overwrite each other.

An agent that rebuilds its own context instead of forwarding `message.context` silently resets the
chain. `notes_agent` did exactly that (it also dropped `session_id` / `origin_channel_id`), fixed
here. `doc_planner_agent` was already correct.

The key is underscore-prefixed to mark it as infrastructure bookkeeping. Verified that it cannot
reach a prompt: `PromptBuilder.build_for_agent` takes named parameters, never the context dict, and
`_execute_sync` spreads only `context["params"]` into the payload.

## Observed on the first day live (2026-08-25)

The sibling change — per-call `mode` on `delegate_to_specialist` — went live in the same
revision. Smart set `mode` on **4 of 4** delegations despite the schema saying to omit it, and all
four agreed with the manifest, so nothing changed behaviourally and UAT was clean.

No guard was added in response. Four samples with zero divergence is not evidence of a problem,
and locking the six ASYNC intents would be defending a hypothesis. What was added is the ability
to see it: the log flags an override **only when it diverges** from the manifest, and carries the
delegation depth. Marking every override would bury the one case that changes behaviour — and that
case (forcing `now` on a 720s document intent) blocks the orchestrator until its own timeout.

Loud rather than silent, by design. The point of the logging change is that it is also
diagnosable.

## Revisit if

A legitimate chain ever reaches depth 8 — raise the cap rather than removing it, and record why the
chain got that long.
