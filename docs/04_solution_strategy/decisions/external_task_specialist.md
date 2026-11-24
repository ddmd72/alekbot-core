# Decision: the external task queue is a specialist, not Smart's own tools

**Status:** Adopted (2026-11-24), owner decision. Spec: `docs/10_rfcs/EXTERNAL_TASK_QUEUE_RFC.md` D14, §5.3. Built on
`decisions/external_task_confirmation_and_alerts.md` (every chat-created task waits for the owner's confirmation; a
finished task reaches the bot as a system alert).

## Decision

Smart has no queue tool. The five tools of the generic external task queue (create, list, get, cancel, list workers)
belong to one LLM specialist, `ExternalTaskAgent` (`src/agents/external_task_agent.py`), behind the internal intent
`work_with_external_tasks`. Smart offers that intent only on the owner's own chat turn or while one of the owner's reminders runs, of a user with the
queue switched on, and writes the specialist a natural-language instruction: the worker's name and the whole brief, or which task to
read or cancel. The orchestrator gives a specialist instructions, not parameters: the intent carries no payload field.

Five rules come with the move:
- **The brief is Smart's, word for word.** The create handler refuses a text that is not a part of the instruction
  after whitespace is normalised (`ERROR_NOT_THE_BRIEF`). The specialist chooses the title and the worker label, never
  the words that leave the system.
- **A chat task waits for the owner's confirmation; a reminder's task does not.** The specialist computes the entry once
  per run from the context (`task_entry`), never from the model. `TaskEntry.CHAT`: the owner is shown the exact text and
  a command only they see, and nothing leaves until they send it; the code reaches the owner's receipt only, never a
  model. `TaskEntry.REMINDER`: queued at once with a tool note and no receipt, because the owner approved that reminder
  when it was set.
- **The specialist repeats the gate in code** before any model call: a run with a task entry, a call chain of itself alone,
  a service, a user, then the service's fresh flag and lock check. The coordinator checks no allowlist, so the offer
  alone is not a gate: a nested Smart or Lelik's `/voice/delegate` naming the intent gets a code-written refusal.
- **The answer is composed by code** from the record of tool calls (every result verbatim, the note, a cut line), and
  the specialist answers success whenever a tool ran, so a receipt is never dropped.
- **Labels only.** A worker's description (free text typed in the Cabinet) reaches no model; a worker's result or
  failure reason read through `get_external_task` stays inside its outside-content wrapper.

Smart's intent line is kept to about ninety words: it is read on every turn the gate holds. The specialist's own prompt
(profile `external_tasks`) and its tool descriptions carry the procedure in full, in the style of the To Do and drive
specialists.

## Why

- **The orchestrator's surface.** Five tool schemas with their descriptions sat in Smart's request on every flagged
  turn; one intent line replaces them. The procedure (which label the owner meant, which task a phrase points at, read
  at most three, ask when ambiguous) runs in the specialist's context instead of Smart's.
- **One pattern for a capability with its own procedure.** The drive specialist (`decisions/drive_specialist.md`)
  already works this way: Smart commissions in natural language, the specialist works through CRUD tools, code builds
  the answer from a pass-through record.
- **What leaves the system stays Smart's words.** A second model between Smart and the worker could reword the brief;
  the verbatim check makes the text the owner confirms exactly Smart's brief.

## Rejected alternatives

- **Keep the tools on Smart.** No extra model call, but the five schemas stay on Smart's every flagged turn and the
  queue's reasoning stays in Smart's context.
- **Structured payload fields** (`worker`, `title`, `text` as `context_schemas` of the intent). Moves the schema back onto
  Smart's delegate tool, which every intent shares; a specialist takes instructions, not parameters.
- **The verbatim rule in the prompt alone.** "Copy the brief word for word" as a rule alone is followed literally only
  most of the time; what must hold is enforced in code.

## Costs

- One more model call per hand-off (BALANCED, thinking low, a lazy agent).
- A brief Smart writes in the wrong language is forwarded as written: the `query_language` clause of
  `PROTOCOL_AGENT_SELECTION` names the brief as an exception to "Always English".
- A skill that names a queue tool by name points at a tool Smart no longer has; a skill names the hand-off instead.
- An external cancellation of Smart's turn after the specialist stored a task loses that receipt, as it did with
  Smart-local tools; the task stays visible in a listing and can be cancelled.

## Verified

`tests/unit/agents/test_external_task_agent.py` (gate, every create waiting, verbatim brief, cut runs keep receipts),
`tests/unit/agents/core/test_smart_external_task_intent.py` (the offer under Smart's gate, Quick never sees it),
`tests/unit/infrastructure/test_external_task_specialist_chain.py` (Smart, coordinator, lazy factory, specialist),
`tests/unit/agents/test_external_task_agent_e2e.py` (every declared field of the two writing tools).

## Revisit if

The hand-off becomes frequent enough that the extra model call matters.
