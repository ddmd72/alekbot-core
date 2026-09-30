# RFC: Skill Authoring — model-written skills and their threat model (Agent Skills phase 2)

**Status:** DRAFT — not approved. Blocked on phase 1 (`AGENT_SKILLS_RFC.md`) passing its trigger-reliability eval, and on the review gates in §1.
**Date:** 2026-09-30
**Depends on:** `AGENT_SKILLS_RFC.md` (format, folder layout, `SkillsAgent`, catalog, `_caller_agent_id`).
**Amends, when approved:** `decisions/standing_directives.md` ("autonomous self-notes rejected permanently"), see §7.

## 1. Mandatory review gates — Fable at every stage

Model-authored skills are a **persistent prompt-injection channel**: text written once is later followed as a procedure, verbatim, in every matching conversation. The owner's standing requirement for this RFC (2026-09-30) is three independent reviews by the most capable model, **Fable** (`claude-fable-5-1`). Each gate blocks the next stage:

| Gate | Reviewed artefact | Reviewer brief | Blocks |
|------|-------------------|----------------|--------|
| **G1 — RFC review** | This RFC, final draft | Adversarial security review: act as the attacker. For each of the six layers (§4), find an input that passes it. Find paths the threat model (§3) misses. Verify every code claim against the repo. | Writing the implementation plan |
| **G2 — Plan review** | The implementation plan | Does every layer in §4 have a task, tests that prove the layer *rejects* the attack (not only that the happy path works), and no task that weakens a layer for convenience? | Starting implementation |
| **G3 — Execution review** | The implemented branch | Whole-branch security review against §3–§4: try the attack cases from G1 against the real code; check nothing bypasses the gates (other callers of `SkillService` writes, other Cloud Task paths, tests mocking a layer away). | Merge |

The cost is accepted by the owner: these reviews are expensive, and a missed injection path is worse. Findings are resolved in the artefact, and the gate is re-run if a finding changed the design. The regular per-task reviews of subagent-driven execution still apply; G3 is in addition to them.

## 2. Design (carried from AGENT_SKILLS_RFC revision 3, with its review findings folded in)

### 2.1 The author

`SkillsAgent` gains a second intent:

| Intent | Mode | LLM | Purpose |
|--------|------|-----|---------|
| `author_skill` | **ASYNC** (Cloud Task) | the skill author | Create, change or delete a skill from a natural-language brief |

- **Always async.** The orchestrator answers that the write has *started* (like the existing ASYNC acknowledgement, `agent_coordinator.py:579-586`), not that it is done. The author posts the outcome itself (§2.6). For Lelik this makes authoring survive a hang-up.
- **The orchestrator does not write the skill text.** It sends:
  - `query` — the brief;
  - optionally `context.skill_material` — results of this request's tools the author cannot find in history. This is capped in size, because Cloud Tasks payloads are limited to 1 MB.
- **Author tier and provider are pinned** in `agent_config.py` (as Consolidation is), PERFORMANCE, and independent of the orchestrator's per-request tier.
  - The loop follows ConsolidationAgent's own tool loop (`consolidation_agent.py:864-903`), not `DelegationEngine`.
  - The only tool is `read_skill(name)`. There is no `read_history`: the session document holds only the hot window.
- **Output:** structured operations — `create`, `update`, `delete`, `restore`, `attach_file` — or `no_op` with a reason code. It follows the Agent Output Format standard: an `OUTPUT_FORMAT_SKILL_AUTHOR` token, `json.loads`, retry on invalid, no regex fallback. `restore` needs a `list_versions` / read-version tool.
- **Prompt profile:** a Firestore blueprint plus `COGNITIVE_PROCESS_SKILL_AUTHOR` and `OUTPUT_FORMAT_SKILL_AUTHOR`. The authoring guidance lives here, not in any orchestrator's catalog.

### 2.2 The author's source material (fixes for the revision-3 review, B2 and M1)

History is saved at the end of `ConversationHandler.handle_message` (`conversation_handler.py:941`), after the Cloud Task has usually started. So the author's input is built from the task payload plus the store:

| Scope | Source |
|-------|--------|
| smart | Session snapshot from the session store **plus** `current_message_parts` from the payload, de-duplicated if the snapshot already ends with that turn |
| tutor | `context["history"]` from the payload. Companion history is written under `write_session_id = platform:channel`, never under the context's `session_id` |
| lelik | `call_context` only (the last 12 exchanges). Never the primary-chat session that Lelik's context also names. **Phase 3 proposal:** post-call authoring on the full transcript, with Lelik only marking "worth keeping" during the call |

- The current turn's final answer is never visible to the author. Anything the orchestrator worked out in this turn must go into `skill_material`.
- **Cost bound:** `full_text` for the last N turns, `text` (summary) for older ones, plus a hard input-token cap. Consolidation deliberately avoids `full_text` for cost.
- An empty history with a non-empty brief is a **failure**, not a `no_op`: `load_session` returns an empty session on error (`firestore_session_store.py:130-132`).

### 2.3 Storage (fix for the revision-3 review, B1)

- Custom skills use the phase 1 folder shape, one immutable folder per version: `skills/<user_id>/<scope>/<name>/v<n>/`.
- **Dedicated bucket** `GCS_SKILLS_BUCKET` (required config → `load_settings()`), with no lifecycle rule. The media bucket deletes everything outside `email_review/` after 30 days (`docs/05_building_blocks/file_storage/README.md:326`); skills there would vanish silently.
- The Firestore index `{prefix}skills/{user_id}:{scope}:{name}` holds `description`, `current` (a version or `null` = tombstone), `next_version`, `account_id` and `updated_at`.
- **Write protocol:** reserve `n` in a transaction → write the complete `v<n>/` → flip `current` only if unchanged. No live folder is overwritten.
  - **On a flip conflict the operation is aborted** and reported. It is not re-applied, because its body was written against the old version.
- Caps: 20 custom skills per scope for Smart and Tutor, 10 for Lelik. Description ≤ 250 chars. `SKILL.md` ≤ 20 KB. File ≤ 1 MB.

### 2.4 Idempotency (fix for the revision-3 review, M2)

Retries come only from instance death, a Cloud Run timeout or an overdue dispatch deadline: `route_message` catches agent exceptions and the worker returns 200.

- The author **persists its operations** under the task's request id (an `author_runs/{id}` document) **before applying any**.
- A retry replays the stored operations and does not call the LLM again: a second LLM run could choose a different name and create a duplicate.
- `dispatch_deadline_s` on the descriptor exceeds the author's timeout, as ALEK's does (`agent_manifest.py:767`).
- A partial application is reported in the marker.

### 2.5 Generic dispatch mechanisms (fix for the revision-3 review, M4)

Instead of special cases per intent in `AgentCoordinator`:
- a `_task_request_id` UUID is stamped in `_execute_async` for every ASYNC enqueue;
- `_caller_agent_id` is copied into the payload;
- `_interactive` is stripped from every Cloud Task payload unless the descriptor lists the intent in `interactive_only_intents`, in which case it is kept, and the intent is rejected before enqueue when the flag is missing;
- `mode_locked_intents` sit on the descriptor, checked before the mode is resolved.

Interactive entry points: ConversationHandler (Slack/Telegram turns) and `LelikAgent.delegate_outcome` (a live call).

Paths to state explicitly:
- **`SmartRetryService`** re-executes a timed-out live turn from a Cloud Task without the flag. **Proposal:** carry the flag, because it is the user's own message.
- **`ask_alek`** lets Smart author into the *smart* scope during a call.
- **A `tell_alek` errand** cannot author, because the flag is stripped.

### 2.6 Marker

- The author posts one line per operation, or the `no_op` / failure reason, to the origin channel **and thread**, and appends it to the origin session's history. That history entry is how the next turn's orchestrator learns what happened.
- `notify_raw` has no `thread_id` and writes no history (`user_notification_service.py:130-163`), so a variant is needed. The existing pattern is NotesAgent's (`notes_agent.py:564`).
- Reasons are enum codes localised through `LocalizationPort`, not free LLM text.
- Each saved operation's line includes a one-line **"what changed"** written by the author and checked by layer 3 (§4).

### 2.7 Observability

- `enqueue_agent_task` propagates trace headers like `enqueue_slack_event` does (`gcp_task_queue.py:57-70`), so an author run hangs under the turn that caused it.
- One Logfire event per operation, with verdicts from every layer. It feeds the revert trigger (§7) and forensics.

## 3. Threat model

**Asset:** the orchestrator's future behaviour. A skill is followed verbatim in every conversation where its trigger matches, including unattended runs.

**Attacker:** anyone who can put text in front of the model:
- an email (daily review, `search_emails`);
- a web page (`search_web`, `fetch_url`);
- a document or file the user opens;
- a message forwarded into Slack/Telegram;
- the output of another specialist.

The attacker cannot type as the user.

**Attack goals once a skill is planted:**
1. **Exfiltration.** Put user data into a `fetch_url` / `search_web` query or into a public HTML page.
2. **Persistence and self-propagation.** Write more skills, standing directives or self-reminders (reminders are deferred execution).
3. **Manipulation.** Bias answers, suppress warnings, steer decisions.
4. **Denial.** Break recurring procedures, for example the daily review.

**Owner's observation (2026-09-30):** nobody reads what the model writes into a skill. So chat approval (`confirm` mode) is **not a security control**. It is a convenience setting, and defences must not depend on a human reading the body.

## 4. Defence layers

The principle: do not rely on the model recognising an attack; make a model mistake inconsequential. Layers 1–3 and 5 are deterministic code; layer 4 is a separate model. An attack must pass all of them.

1. **Provenance (deterministic).** Code labels every input segment:
   - `USER_TYPED` — user messages;
   - `MODEL` — assistant turns, derived and possibly contaminated;
   - `EXTERNAL` — `*_context` blocks and `rich_content` inside `full_text` (`conversation_handler.py:842-862`), files, `skill_material`, specialist output.

   `EXTERNAL` is removed from the author's material by default. `skill_material` is present only inside a fenced "data, not instructions" block. Heuristics downgrade parts of a user message to `EXTERNAL`: quoted blocks, forwarded messages, long pastes, attachments. A paste is the known weak spot of this layer.
2. **Mandatory user quote (deterministic).** Every operation must cite the user's words that motivated it. Code verifies the quote appears **verbatim** in `USER_TYPED` segments (after the downgrade heuristics). No quote, no write. A web page cannot forge what the user typed.
3. **Content constraints (deterministic).** A scan of the new body and the "what changed" line:
   - URLs only if they appear in `USER_TYPED`;
   - no instructions to send, publish, write memory, create directives, create reminders or author skills;
   - secret patterns;
   - `SecurityPort` injection patterns.

   Violation → reject.
4. **Security reviewer agent (LLM, a different provider from the author).** It receives the diff (old body → new body), the quotes and the provenance map, and returns `approve | reject` with a reason. It runs before the pointer flip. Provider diversity reduces correlated blind spots: one crafted text is less likely to fool two different model families.
5. **Blast-radius limits at use time (deterministic, in `DelegationEngine`).** In a request where a *custom* skill was loaded, the engine refuses `author_skill`, directive-creating intents and reminder-creating intents for the rest of that request. This stops self-propagation and deferred execution even if a malicious skill is followed. A legitimate "update this skill" moves to the next turn.
6. **Forensic trail.** Each version folder stores `review.json`: quotes, layer verdicts, reviewer reason, request id and caller. Nothing is ever destroyed by the model (tombstones, immutable versions), so an incident can be reconstructed and rolled back.

**Residual risk, stated for G1 to attack:** a long paste inside a user message that the downgrade heuristics miss, which also contains a plausible "user quote", plus a reviewer fooled by the same text, plus a payload whose harm does not need the actions layer 5 blocks (e.g. manipulation, goal 3).

## 5. Owner decisions still open

- **Purge.** "No path destroys content" holds for the model. The proposal is an owner-only hard delete (script or Cabinet) for "delete all my skills" and account deletion; the model can never purge.
- **Scope granularity** (phase 3): per agent and per session/binding.
- **Rate limits:** set from observed usage, after launch.
- **Lelik:** in-call authoring from `call_context`, or post-call on the full transcript (the proposal).
- **Consolidation duplication:** accept and measure in v1, or mark skill-producing spans in history so consolidation skips them.

## 6. Out of scope

- Script execution.
- A Cabinet editor.
- Consolidation curating skills.

## 7. Revision of "autonomous self-notes rejected permanently"

When this RFC is approved, `decisions/standing_directives.md` is amended. Autonomy is allowed for procedures only, behind §4. The self-notes that failed differ in these ways:
- they were the agent grading its own behaviour;
- they were injected everywhere;
- they were written by whichever tier ran;
- they were unversioned and unreviewed.

**Revert trigger**, measured from the §2.7 events and reviewed one month after launch. If any of the following holds, pause autonomous authoring:
- more than 3 restores;
- more than ~2 writes per active day, sustained over a week;
- any skill the owner did not recognise;
- any layer-4 rejection that layers 1–3 did not also catch. The last one shows the deterministic layers leak.
