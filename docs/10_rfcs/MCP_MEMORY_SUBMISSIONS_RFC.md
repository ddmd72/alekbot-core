# RFC: External memory submissions over MCP — the memory inbox

**Status:** Implemented on `feat/memory-inbox`. Accepted by the owner 2026-11-25.
**Where the code and this text differ once built, the code is the truth.**
**Related:** `MEMORY_FACT_CONFIRMATION_RFC.md` (the owner-confirmed write rule this extends),
`decisions/memory_facts_owner_confirmed.md` (why user-role text is the owner's own words),
`REMOTE_MCP_SERVER_RFC.md`, `DRIVE_MCP_CONNECTOR_RFC.md` (token-served connector texts),
`EXTERNAL_TASK_QUEUE_RFC.md` §5.4 (a non-default scope on its own resource), `FACT_LIFECYCLE_RFC.md` (lease, review,
`purge_after`), `decisions/worker_task_dedup.md`, `decisions/mcp_tool_schema_liberal_input.md`,
`docs/05_building_blocks/memory/README.md` (§2.7 dates, §3 step 8 mail, step 9b profile).

`decisions/` = `docs/04_solution_strategy/decisions/`. Written before the build: a name marked "new" below exists in the code now; where this text and the code differ, the code is the truth. Owner's map: `docs/05_building_blocks/memory/README.md` §5.7; decision record: `decisions/memory_inbox_external_submissions.md`.

## 1. Problem

An external assistant (Claude in claude.ai or Claude Code) already reads the owner's memory through the facts connector
(`get_user_context`, `src/composition/mcp_setup.py:281-348`). In its own conversations it learns things about the
owner that never reach the exocortex: the owner said them to another assistant, not to Alek. Today there is no way back.

Three constraints shape any way back:

- **User-role text is the owner's own words.** The consolidator reads every user-role message as the owner speaking
  (`decisions/memory_facts_owner_confirmed.md`; rule `Domain_Scope`,
  `prompts_snapshot/tokens/system/CONSOLIDATION_POLICIES.groovy:15`). Text another model wrote must never be put there.
- **Heavy noise is expected.** An external model cannot be taught precisely what to send. Its description narrows the
  stream; it will still retell what memory already holds, send trivia, and send things about the world.
- **The bot never writes memory itself** (root `CLAUDE.md` traps; memory README §5.1). Only an owner-confirmed path may
  reach the consolidator.

## 2. Decision

A **memory inbox**: a dedicated consolidator stage with its own store, its own passes and its own instructions, shaped
like the mail stage (memory README §3 step 8).

1. **Submit.** The external model calls one write tool, `submit_user_information` (new), on a new MCP resource
   (§5). Each item is stored as a `received` submission of the token's user (§6). Nothing is searchable, nothing is in
   a prompt.
2. **Triage** (new pass, §7). At most once a day, and only when new submissions arrived, the consolidator reads them
   against the owner's real memory and turns the worthwhile ones into **candidates**: one thought each, coherent full
   sentences in the owner's UI language, with approximate times made absolute. Everything else is discarded.
3. **The owner checks each candidate in the Cabinet** (§8): edits it if needed, then "True" or "Not true". One at a
   time; no batch approval. "True" attests **authenticity only** ("this is true / I said it"); it is not an order to
   save.
4. **Final pass** (new pass, §9). One hour after the owner's last confirmation, the consolidator reads the confirmed
   candidates as the owner's own statements and applies every normal fact rule. It may still discard a candidate
   (trivial, irrelevant, already known) or resolve a contradiction. Only this pass writes facts.

External text never enters a chat session, as a user-role message or otherwise. The one chat line this feature sends
(§11) carries a count, is posted literally and is not written to history.

## 3. Non-goals and anti-goals

- **No batch approval.** The Cabinet offers one decision per candidate.
- **No direct fact writes.** Neither the tool, nor the triage, nor the Cabinet writes a fact. The final pass is the only
  writer, through the normal tools under the normal rules.
- **External text is never a user message**, in any session, in any form (decision 2 above).
- **No voice and no Lelik path.** The write tool lives on its own resource (§5.1), which voice tokens never verify on.
- **No Smart access to the inbox.** No intent, no local tool, no prefetch reads it. Smart sees a submission only after
  the final pass made it a fact.
- **No new defence machinery.** The owner's check is the gate. Triage marks submitted text as data the same way the
  mail stage marks its candidates (§7.3); no classifier, no security port call, no quota beyond a cost bound (§5.4).
- **The external model is not asked to read memory before it submits** (§13).

## 4. One submission, end to end

1. The owner talks to an external assistant connected through the memory inbox connector. It calls
   `submit_user_information` with one or more items. The server stamps each with the receipt time, the token's user
   and account, and the OAuth client's registered name, and stores it `received` (§6). The result tells the model the
   owner reviews everything and nothing is searchable yet (§5.3).
2. Once a day the inbox sweep (§10.1) finds the user has `received` submissions and the triage interval has elapsed,
   and enqueues one `memory_inbox_triage` task.
3. The triage task takes the account's memory-write lease and runs the triage pass (§7). Each submission ends
   `discarded` or `triaged`; each worthwhile thought becomes a `candidate` with a 30-day answer window.
4. If the triage made N > 0 candidates, the owner gets one literal chat line in the owner's UI language with the count and the
   source (§11).
5. The owner opens the Cabinet's Memory section (behind the facts PIN gate when the owner has one) and answers each candidate:
   "True" (with the owner's edits) → `confirmed`; "Not true" → `rejected`. Each "True" sets the final pass one hour ahead
   (§10.3).
6. One hour after the last "True", the final task takes the lease and runs the final pass (§9) over every `confirmed`
   candidate. Each ends `consolidated`. Facts it wrote are searchable at once and reach the baseline after the refresh.
7. A candidate nobody answers for 30 days becomes `expired`. Records meant to vanish carry `purge_after` (§6.3).

## 5. The MCP tool

### 5.1 Where it lives: its own resource (author's call)

**Recommendation: a fourth MCP resource, `/memory-inbox-mcp`, with its own scope `memory_submit`** (both new), built the
way the task queue's worker resource is (`src/composition/tasks_mcp_setup.py:1-41`): a resource-server-only
`TokenTextFastMCP` beside the facts app, which stays the authorization server.

Why not a second scope on the facts resource `/mcp`:

- **One scope per resource is the binding rule.** `bind_resource` maps each resource to exactly one scope and refuses any
  other scope requested for it (`src/domain/mcp_resource.py:56-93`, the check at `:77-79`); `SCOPE_OF` holds one string
  per resource (`src/composition/mcp_setup.py:61`).
- **FastMCP has no per-tool scope.** `required_scopes` is app-wide (`mcp` 1.30, `server/fastmcp/server.py:872-876`), and
  the protected-resource metadata advertises exactly those as `scopes_supported` (`:962`). A write scope on `/mcp`
  would need a filtered `tools/list`, a per-call scope check and a two-scope binding.
- **That would hand write access to every new facts connector.** claude.ai registers the one scope a resource's
  metadata advertises (`docs/05_building_blocks/remote_mcp_server/README.md:141-142`). Advertising two on `/mcp` makes
  every re-added facts connector a write connector: exactly the silent gain the owner ruled out.
- **Voice tokens verify on the facts resource** (`src/composition/voice_mcp_setup.py:83-95`). A write tool there would
  have to be fenced off from Lelik; on its own resource it never is reachable.

What changes, all on the existing pattern:

| Where | Change |
|---|---|
| `src/domain/mcp_resource.py` | `MCPResource.MEMORY_INBOX`, `MEMORY_INBOX_PATH = "/memory-inbox-mcp"` (not under `/mcp/`, which the facts app owns, as `TASKS_PATH` notes at `:24`), `memory_inbox_resource_uri`, `is_memory_inbox_grant`; `bind_resource`'s no-resource branch selects it when the scopes are exactly its own |
| `src/composition/mcp_setup.py` | `MEMORY_INBOX_SCOPE = "memory_submit"` in `SCOPE_OF`; in `valid_scopes`, **never** in `default_scopes` (`:114-115`), as `external_tasks` |
| `src/services/mcp_authorization_service.py` | serve the inbox URI when `scope_of` holds its scope, as the tasks URI (`:193-195`) |
| `src/web/mcp_dispatcher.py` | `mcp_owner` routes the path and its protected-resource metadata (`:33-46`) |
| `src/web/mcp_consent_app.py` | an inbox grant gets its own wording (beside `is_drive_grant` / `is_tasks_grant`, `:114-123`): the connector can only send records for the owner's review and reads nothing |
| new composition module | the inbox app: an OAuth-only token verifier with `aud` = the inbox URI (no voice route), the tool, `ConnectorTextService` texts |

Already-connected clients gain nothing: their registered scope is fixed at registration
(`mcp_authorization_service.py:258-260`). The owner adds the inbox connector in claude.ai as a new connector, and
re-adds the facts connector to pick up the changed `get_user_context` description (§5.5), because claude.ai caches
`tools/list` (`decisions/mcp_tool_schema_liberal_input.md`, "Client-side caching caveat").

### 5.2 Input

One tool, `submit_user_information`, one parameter `items`: a list of objects, each with four string fields. Accept
liberally, advertise strictly (`decisions/mcp_tool_schema_liberal_input.md`): defaults instead of `Optional`, a
`description` on every field, **no** schema constraint (`maxItems`, `maxLength`, `pattern`, `additionalProperties`).
A `BeforeValidator` (pure, in `domain/`) absorbs every plausible shape.

| Field | Meaning | Normalisation |
|---|---|---|
| `information` (required) | one thing about the user, free text | trimmed; empty → the item is dropped (counted) |
| `basis` | how the model knows it | mapped to `said` (the user said it directly), `document` (from a document, file or image the user showed), else `inferred`. Missing or unrecognised → `inferred`, the least trusted |
| `said_when` | when the user said it, if not today | free text, approximate is fine; empty = in this session |
| `happens_when` | when the thing happened or happens, in words as said | free text; empty = not stated |

- A bare string is one item with only `information`; a list of strings is that many items; a JSON-encoded array in a
  string is parsed; explicit nulls are empty.
- **Caps cut, never fail** (§5.4): items past `MAX_ITEMS_PER_CALL` are dropped, and each text field is cut at its char
  cap. Every cut is counted in the log line and named in the result.
- The only `isError` results: authentication, a locked account (`ACCOUNT_LOCKED_TOOL_ERROR`, as the facts tool at
  `mcp_setup.py:319-321`), a store failure, and a call with no usable item at all.
- The emitted schema is pinned by a test against the real `list_tools()` output, as `tests/unit/composition/test_mcp_setup.py`
  pins the facts tool: an inline item object, no `anyOf` with `null`.

**The account comes from the token** (`AlekAccessToken.user_id`, `.account_id`, `src/composition/mcp_sdk_oauth_provider.py:72-82`),
as the facts tool takes it (`mcp_setup.py:314-315`). Submissions belong to that user.

### 5.3 Texts

The tool description, every field description, the server instructions and every result line come from two new
Firestore prompt tokens, `MCP_MEMORY_INBOX_TOOL_TEXTS` and `MCP_MEMORY_INBOX_RESULT_TEXTS`, through
`ConnectorTextService` (`src/services/connector_text_service.py:35`) and `TokenTextFastMCP`
(`src/composition/token_text_fastmcp.py`), as the drive and tasks connectors do (`tasks_mcp_setup.py:86`). No inline
text; a first load that fails leaves the app unbuilt (its path answers 404), the facts and drive apps run on.

The texts follow the root rule "explains, then rules", with **no examples**. The description says, in this order:

1. **What it feeds.** The user's exocortex keeps a long-term memory of their life; this tool hands it things the user
   told *this* assistant.
2. **What happens next.** Nothing sent is stored as memory. The owner reads and checks every record in person; a separate
   pass first compares each one with what memory already holds and drops what it knows or what is not about the user.
3. **The loop guard, with its reason.** Send only what the user said or showed in this session. Never send back what
   `get_user_context` returned: memory already holds it, and sending it back closes a loop in which memory confirms
   itself and breeds duplicates. Something the model worked out with the help of retrieved memory goes with basis
   `inferred`.
4. **The shape.** One thing about the user per item, in plain sentences. Precision is not required: naming uncertainty
   ("about", "early in the year", "not sure") is better than inventing a date.

The success result states how many items were received and, when any, how many were cut or not accepted today; that
the owner reviews them before anything reaches memory and nothing is searchable now; and that the same items should not
be sent again. It never says "saved".

### 5.4 Limits (author's proposals)

| Limit | Value | Why |
|---|---|---|
| `MAX_ITEMS_PER_CALL` | 20 | the draft size of `propose_memory_facts` (`src/domain/memory_fact.py:15`) |
| `MAX_INFORMATION_CHARS` | 1000 | a raw item may be longer than a candidate; triage condenses it |
| `MAX_WHEN_CHARS` | 200 | each time field is a phrase |
| `RECEIVED_PER_DAY` | 100 per user per owner-local day | a cost bound on a looping client, not a defence: items past it are not stored and the result says so (not an error) |

The day count lives on the user's inbox state document (§6.2), incremented in the same transaction as the write.

### 5.5 The read tool's description

The same no-retell sentence (point 3 above) is added to `get_user_context`'s description, where the loop starts. That
description is a git constant today (`_TOOL_DESCRIPTION`, `src/composition/mcp_setup.py:141-151`), so the sentence ships
by deploy and the facts connector is re-added to see it. Moving the facts app to token texts is not part of this RFC.
The sentence is worded so it holds for a client that has no inbox connector: records returned here are never to be sent
back into the user's memory by any tool.

### 5.6 Each call

Like the drive and tasks callers: the user from the verified token; the account lock read fresh
(`InactiveUserCheck.is_inactive`, `src/services/inactive_user_check.py:23`); normalise; one transaction storing the
items and bumping the day count; one span `mcp.memory_inbox.submit` and one log line (§12). The new tool is a new entry
point: `tests/unit/test_locked_user_entry_points.py` (`ENTRY_POINTS`, `:41`) gains a row and its probe, and the
classification test (`:12-16`) gains the new paths.

## 6. The store

### 6.1 One collection, one status field (author's call)

`{prefix}memory_inbox` (new `EnvironmentConfig.memory_inbox_collection`), two kinds of document under one `status`
field. A candidate is its own document because triage may **split** one submission into several thoughts (decision:
one thought per candidate) and may serve several submissions with one candidate; one document per submission could not
hold that. The status values do not overlap between the kinds, so every query filters on `status` alone.

```
submission:  received ──► triaged      (cited by at least one candidate)
                     └──► discarded    (triage reason, or undecided after TRIAGE_MAX_OFFERS)
candidate:   candidate ─► confirmed ─► consolidated
                      ├─► rejected
                      └─► expired      (ANSWER_WINDOW passed)
```

**Submission fields:** `user_id`, `account_id`, `status`, `received_at`, `client_id`, `client_name` (the registered
client's name, `normalize_client_name`, `src/domain/mcp.py:48`), `information`, `basis`, `said_when`, `happens_when`,
`cut` (which fields were cut), `triage_offers`, `triaged_at`, `discard_reason`, `purge_after`.

**Candidate fields:** `user_id`, `account_id`, `status`, `created_at`, `answer_by` (= `created_at` + `ANSWER_WINDOW`),
`language` (the UI language it was written in), `source_ids` and `sources` (a copy of the raw submissions' text and
fields, for the card's collapsed view; the copy lets the submissions be purged on their own clock), `text`,
`happens_when`, `said_when`, `basis`, `draft` (`directive`, `temporal_class`, `context_priority`, `expiry`, §7.4),
`relation` (`kind`, `fact_id`, `fact_text`), `proposed` (triage's own text and fields, kept when the owner edits),
`edited`, `confirmed_at`, `rejected_at`, `final_attempts`, `consolidated_at`, `outcome` (`done` or `gave_up`, §9.4),
`purge_after`.

No field is named `expires_at` (root `CLAUDE.md` TTL trap).

### 6.2 The state document

`{prefix}memory_inbox_state/{user_id}` (new `memory_inbox_state_collection`): `last_triage_at`, `last_triage_failed_at`,
`final_due_at`, `final_attempts`, `last_final_at`, `received_day` (`{day, count}`, §5.4). No TTL. This is where rhythm
lives (§10), never in a Cloud Tasks name.

### 6.3 Retention (author's proposals)

| Record | `purge_after` | Why |
|---|---|---|
| submission, `discarded` or `triaged` | triage time + 30 days | enough to debug a triage; the candidate keeps its own copy |
| candidate `expired` | + 30 days | nobody answered; a resubmission may make a fresh candidate |
| candidate `rejected` | + 180 days | shown to triage so the same noise does not come back; a client's own memory repeats an item for months, past the review's 90-day retention (`QUESTION_RETENTION`, `src/domain/fact_review_question.py:26`) |
| candidate `consolidated` | + 180 days | shown to triage as "already considered", so a candidate the final pass chose not to store is not offered again |
| `received`, `candidate`, `confirmed` | none | still open |

A Firestore TTL policy on `purge_after` deletes them, as for the review collections (memory README §5.6 "Retention").

### 6.4 Indexes

Composite indexes (`config/firestore.indexes.json`, `READY` before the code ships — root `CLAUDE.md` trap):

- `(user_id, status, received_at ASC)` — the triage batch, oldest first;
- `(user_id, status, created_at DESC)` — the Cabinet list, the triage's shown lists, the final batch;
- `(status, answer_by ASC)` — the sweep's expiry query across users.

The sweep's "who has `received` submissions" is one equality on `status` with a projection (single-field index).

### 6.5 Port

A new port `MemoryInboxRepository` (Firestore boundary, with an in-memory fake for service and handler tests) and its
Firestore adapter, as `MemoryFactDraftRepository`. Justified on the same ground: a system boundary with a test
substitute.

## 7. The triage pass

### 7.1 Where it runs

A new `ConsolidationAgent` task, `triage_memory_inbox`, beside `consolidate_email` (`can_handle`,
`src/agents/consolidation_agent.py:893-901`; dispatch `:949-969`). A new `PassKind` `"triage"` (`:392`) declares its
tools in `_get_tool_declarations` (`:2283`); a new pass label `inbox_triage` keys the stats and alert lines (§3.11 of
the memory README). The triage writes no fact, so no fact-writing tool is declared; its `PassActor` row exists only
because the loop and the stats key on the actor (`_run_consolidation_loop`, `:1749-1790`).

The pass is driven by a new service (§10.2) through the coordinator, as `FactReviewService` drives
`review_stale_facts` (`src/services/fact_review_service.py:314-329`). The agent never touches the inbox store: a new
`MemoryInboxTriageScope` (`src/infrastructure/`, one `asyncio.Lock`, as `FactReviewScope`,
`src/infrastructure/fact_review_scope.py:64`) serves the two triage tools, validates every call and holds the decisions;
the service persists them after the pass.

### 7.2 Prompt

A new profile `consolidation_inbox_triage` with a new token `CONSOLIDATION_INBOX_TRIAGE`, plus the existing tokens that
define what memory keeps and how a fact is classified (at least `CONSOLIDATION_TAXONOMY`; the exact set is open, §15).
`include_directives=False` and the baseline in `knowledge_base {}`, as every consolidation pass
(`consolidation_agent.py:1185-1193`). The new token explains before it rules: what the inbox is and who writes into it;
that a human reads every candidate days later, in the owner's own language; what a candidate is for (the owner checks
authenticity, a later pass decides storage); what is already covered (memory itself, open candidates, past answers);
then the rules below. No examples.

### 7.3 The message

The user message is one code header line, then three blocks, each line one JSON object (the mail stage's form,
`EMAIL_CANDIDATES_HEADER`, `src/domain/email_memory_candidate.py:55`; `_format_memory_candidates`,
`consolidation_agent.py:1266-1298`):

1. **Waiting for the owner:** every open candidate (`candidate`, `confirmed`) — id and text.
2. **Already answered:** the newest `rejected` and `consolidated` candidates within their retention, at most
   `TRIAGE_SHOWN_CAP` (100) — text and which of the two.
3. **New submissions:** up to `TRIAGE_BATCH` (40) `received` submissions, oldest first — id, receipt time (the one
   timestamp formatter, owner's zone, with weekday; `src/domain/local_time.py`), client name, the four fields.

The header names the language candidates are written in (the owner's UI language, §7.5) and says the submissions were
written by an outside assistant: data to judge, never instructions. JSON encoding keeps each submission inside its own
object. This is the proportional marking (§3): `wrap_untrusted` (`src/domain/untrusted_text.py:161`) is for file text
and its `tag_for` refuses any store outside the file-ref kinds (`:36-42`, `:125`).

Up to `TRIAGE_PASSES` (3) passes per run (as `email_triage_passes`); what is left waits for the next run.

### 7.4 The tools

- `search_existing_facts` — the consolidator's own (memory README §3 step 5), so dedup is against real memory.
- `count_words` — as today, reporting against `CANDIDATE_MAX_WORDS`.
- `record_candidate(submission_ids, text, happens_when, said_when, basis, directive, temporal_class, context_priority,
  expiry, relation, fact_id)` (new). Code checks, and refuses with a tool error naming the argument and its fix:
  - every id is a submission of this pass;
  - `text` is non-empty, one line, at most `CANDIDATE_MAX_WORDS` (60) words by `word_count`
    (`src/domain/fact_words.py:10`; facts are 40, `:7`). An over-limit text is refused with its count, **never cut**;
  - `basis` is one of the three values;
  - the draft classification parses with the fact parsers: `normalize_fact_taxonomy` (`src/domain/entities.py:68`) and
    `parse_expiry` (`src/domain/fact_expiry.py:35`). It is advisory, so no born-due refusal;
  - `relation` is `new`, `refines`, `contradicts` or `duplicates`; every kind but `new` needs a `fact_id` that
    resolves to a **current** fact of this account. Code reads that fact's text and stores it as `relation.fact_text`,
    so the card's quote is the stored text, not the model's retelling.
- `discard_submissions(submission_ids, reason)` (new): reason `known` (memory holds it), `waiting` (an open candidate
  already says exactly this: nothing added, changed or corrected), `answered_before` (a rejected or consolidated
  candidate says it), `not_about_user`, `trivial`, `unclear`. The reason is for counts only.

**Triage never edits, merges or closes an open candidate** (owner, 2026-11-25). A submission that adds to, changes,
corrects or contradicts a candidate still waiting for the owner becomes **its own candidate**, with its own `said_when`.
The date of a statement is what ranks it: "yesterday I did it one way, today another" is two true statements, not one
wrong card. The owner answers each card on its own, and the final pass reconciles them by date (§9.2). The ids in block
1 of the message serve only the `waiting` decision.

One submission may be cited by several candidates (a split). A submission may not be both cited and discarded: the
second call is refused. At the end of the pass, a submission with no outcome stays `received` with `triage_offers` + 1;
at `TRIAGE_MAX_OFFERS` (2) it is `discarded` with reason `undecided`. Decisions recorded before a turn-cap or a failure
stand: each is independent.

### 7.5 What the token tells triage to do

- **Discard what memory knows.** This is the code-side half of the loop guard: an item retold from `get_user_context`
  finds its fact and is discarded `known`. The one exception is a restatement whose value is its newer date: a fact with
  a horizon the user stated again. That candidate carries relation `duplicates` and lets the final pass apply the
  extension rule (`CONSOLIDATION_TAXONOMY.groovy:394`).
- **Discard what is waiting or answered** (blocks 1 and 2 of the message).
- **One thought per candidate**; split a compound submission; condense, never invent.
- **Language:** the candidate's text and its two time fields are written in the owner's UI language, resolved by
  `LanguageResolver.resolve_ui_language` (`src/services/language_resolver.py:41`: user `preferred_language`,
  `src/domain/user.py:303`, then the account default, then the system default).
- **Times become absolute-approximate, anchored on the receipt time.** The owner reads the card days later and the
  final pass runs later still, so a relative time is turned into an approximate absolute one ("last week" received in
  late November becomes "mid-November, approximately"), never a precise date the submission did not give.
- **The draft classification** is what the consolidator itself sets on `create_fact`: `directive`, `temporal_class`,
  `context_priority`, `expiry` (memory README §3 step 5). Never a domain: the profiler sets domains after the write
  (§2.1, step 9b).

## 8. The Cabinet card and its API

### 8.1 The card

In the Memory section (`src/web/static/cabinet-memory.js`), above the facts, as one collapsed row like the review
questions (`:233-250`, `:435-446`): "From your assistants (N)". Each card:

- **Editable:** text; `happens_when`; `said_when` (both free text, no date pickers: imprecision stays honest); basis as
  a three-way switch.
- **Read-only:** the source (client name) and receipt date; the draft classification in words (directive or not,
  temporal class, priority, expiry); the relation to memory (new / refines / contradicts / duplicates) quoting the stored
  fact's text as stored (English); the raw submission, collapsed.
- **Buttons:** "True" (confirm; sends the owner's edited fields when the owner edited) and "Not true" (reject). The page says
  under the buttons that "True" means "this is true / I said it", and that memory still decides what to keep.

No batch selection, no "mark all". A confirmed candidate leaves the list; a line shows how many are confirmed and
waiting for memory. No undo in this RFC: after "True" the owner corrects through chat or the fact page.

### 8.2 Routes

| Route | Does | Answers |
|---|---|---|
| `GET /api/user/memory-inbox` | the session user's `candidate` documents, newest first, and the count of `confirmed` | 200 |
| `POST /api/user/memory-inbox/<id>/confirm` | body: any of `text`, `happens_when`, `said_when`, `basis`; a transaction moves `candidate → confirmed` (stores the owner's fields, keeps triage's in `proposed`, sets `edited`), then sets the final pass (§10.3) | 200; 400 over `CANDIDATE_MAX_WORDS`, an empty text or a basis outside the three; 403 another user's; 404 unknown; 409 not waiting (answered, expired) |
| `POST /api/user/memory-inbox/<id>/reject` | `candidate → rejected`, `purge_after` set | 200; 403; 404; 409 |

- Each carries `@auth_required` and `@facts_unlock_required` (`src/web/user_cabinet_app.py:198-199`), as every facts
  route: a user with a facts PIN and a session not unlocked gets 403 `facts_locked`, which the page already handles
  (`cabinet-memory.js:74-80`).
- Only the session user's own candidates are listed or answerable (the check `holds_other_users_question` makes for
  review questions, `src/domain/fact_review_overview.py:63`).
- The routes are `/api/` routes, so the locked-user classification test counts them behind the Cabinet gate
  (`test_locked_user_entry_points.py:12-16`); no new probe row.
- The owner's text is the owner's own: no security check, the same word limit as triage. A failed enqueue of the final task
  after a stored confirmation still answers 200 (logged); the sweep's backstop finds it (§10.1).
- **Delta 2026-11-25, owner: frequency choice and connected apps in the Cabinet.** Four more routes in the same module,
  same decorators: `GET`/`PUT /api/user/memory-inbox/settings` (`{triage_interval_days, choices}`; the PUT takes 1, 2, 3 or
  7, else 400 `invalid_interval`) and `GET /api/user/memory-inbox/apps` / `DELETE /api/user/memory-inbox/apps/<grant_id>`
  (the live `memory_submit` grants through `MCPAuthorizationService.resource_grants`, the port's `list_resource_grants`; a
  grant not among them answers 404). The inbox tool re-checks its grant on every call, so a revoke refuses the next one at
  once. No limit, backlog rule or triage change came with it.
- **Delta 2026-11-26, owner: the place is Settings → Connections, with no PIN.** The settings and apps surface is one
  row, "Memory from assistants", in Settings → Connections (CABINET_REDESIGN_RFC §5.1), not a block in Memory; the four
  routes drop `facts_unlock_required` (connection settings, not facts) and keep `auth_required`. The candidate routes keep
  the PIN.
- The routes live in their own blueprint module beside `cabinet_external_workers.py`, not in the 2,200-line
  `user_cabinet_app.py`.

## 9. The final pass

### 9.1 Where it runs

A new `ConsolidationAgent` task, `consolidate_memory_inbox`, modelled on `_consolidate_email_cards`
(`consolidation_agent.py:1170-1253`): a new profile `consolidation_inbox` (the six `CONSOLIDATION_*` tokens plus a new
`CONSOLIDATION_INBOX_CANDIDATES`, as `prompts_snapshot/profiles/consolidation_email.yaml` adds
`CONSOLIDATION_EMAIL_CANDIDATES`); the normal fact tools; a new pass label `inbox` whose `PassActor` takes the
conversation row, as `EMAIL` does (`src/domain/pass_actor.py:89-116`): the model's dates count, a born-due expiry is
refused with "discard or rewrite as history".

The message: a header line, then one JSON object per confirmed candidate (up to `FINAL_BATCH`, 20, per pass, passes
while the lease allows): `candidate_id`, `text`, `happens_when`, `said_when`, `basis`, `received`, `confirmed`, `edited`,
`draft`, `relation` (`kind`, `fact_id`, `fact_text`), ordered by receipt time, oldest first. `said_when` is free text
(the owner may have edited it), so code cannot sort by it; every object carries it, and the token tells the model to
rank the statements by `said_when` (§9.2). **Never the raw submission**: the owner confirmed the
candidate, not the raw text.

### 9.2 What the new token says

- **Whose words these are.** Each candidate was written by an outside assistant, condensed by triage and confirmed by
  the owner as true. The owner's confirmation makes it **the owner's own statement**; the echo-trap rule
  (`CONSOLIDATION_TAXONOMY.groovy:201`, `CONSOLIDATION_POLICIES.groovy:15-20`) is about the assistant recalling memory in
  a chat and does not apply here — the same carve-out the mail token makes for mail
  (`CONSOLIDATION_EMAIL_CANDIDATES.groovy:26`). For the extension rule a confirmed restatement counts as the owner
  saying the fact again.
- **These are dated excerpts from conversations** (owner, 2026-11-25). Each candidate condenses what the owner told an
  outside assistant at one moment, and `said_when` is that moment. Two candidates that disagree, or a candidate that
  disagrees with a stored fact, are usually not an error but a change over time: the later statement is the current
  state and the earlier one is history. The token explains this before the conflict rules apply, so the pass updates
  with history or archives by date, and invalidates ("never true") only when a candidate says the earlier statement was
  wrong.
- **Confirmation is not an order to save.** Every normal rule applies: search first; discard the trivial, the
  irrelevant and the already known; resolve contradictions by the conflict rules; atomise to the 40-word fact limit.
- **The draft is advisory**; the pass decides `directive` and the three axes anew. `basis` `inferred` stays usable: the
  owner confirmed the thing is true.
- **Language:** the candidate is in the owner's UI language; the stored fact is composed by the usual rules (English).
- **Dates:** see §9.3.

### 9.3 `reported_date` (author's call, against memory README §2.7)

`reported_date` means "when the owner last stated it" and code owns it per pass (`src/domain/fact_write_context.py`).

- **It is when the user said it to the outside assistant**: `said_when` when given (triage made it absolute), else the
  receipt time. The model sends it per candidate, as the mail token makes the model copy each mail's `date`.
- **Not the confirmation time.** The owner's confirmation attests authenticity only (§2, step 3): "I said it" is not
  "it holds today". Taking the confirmation time would make a months-old statement look fresh and push its horizon out.
  This is the deliberate difference from a review answer, where "still true" is a restatement and the answer time is
  used (memory README §2.7 "Review and owner writes").
- **The span** (`FactWriteContext.for_pass`, `src/domain/fact_write_context.py:42`; the clamp `clamp_to_span`, `:113`;
  `reported_date_for_new`, `:138`) runs from the pass's oldest receipt time minus `SAID_WHEN_LOOKBACK` (365 days, author's
  proposal) to its newest receipt time. A date after receipt is impossible and is clamped down; a wrong guess far in the
  past is bounded. A missing date falls to the span end, the newest receipt; the token tells the model always to send
  one.

### 9.4 After the pass

- **Marking.** A pass that ended with its final report marks every candidate it was sent `consolidated`, whatever the
  model did, as the mail stage marks its mail (`consolidation_agent.py:1225-1226`). A pass that raised or used all its
  turns leaves them `confirmed` with `final_attempts` + 1, because the owner confirmed them and silent loss is worse than
  a repeated pass (a retry searches and finds what it already wrote, as a Stage 1 retry does, memory README §6). At
  `FINAL_MAX_ATTEMPTS` (3) a candidate is marked `consolidated` with outcome `gave_up` and one 🚨 is posted.
- **Baseline:** refreshed after a pass that wrote, and in a `finally` after a failed one that wrote
  (`consolidation_agent.py:1239-1244`).
- **No Stage 2a, no Stage 2b** (author's call): the mail stage runs neither (`_handle_consolidate_full`, `:1310-1384`);
  candidates arrive atomised and deduplicated by triage, and a directive the pass creates is reviewed by Stage 2b of the
  next ordinary batch, which runs on every batch (memory README §3 step 7).
- **Profile step:** inside the lease after the pass, as `ConsolidationService._consolidate_leased`
  (`src/services/consolidation_service.py:258-272`) and `FactReviewService._daily` (`fact_review_service.py:325`) run
  `FactProfileService.run` (`src/services/fact_profile_service.py:226`).

## 10. Rhythm and triggers

Rhythm comes from the state document (§6.2), never from a Cloud Tasks name: a name dedupes only while its task is
queued or running and for about an hour after (`decisions/worker_task_dedup.md`; `docs/07_deployment/SCHEDULERS.md:56-59`).

### 10.1 The daily sweep (author's call: its own Scheduler job)

A new Cloud Scheduler job sends `memory_inbox_sweep` once a day, 08:30 Europe/Madrid (before the 09:00 billing summary
and the 09:30 janitor, so triage leases are mostly released before the review starts). Zero LLM, no lease, idempotent:

1. **Expire:** candidates with `status == candidate` and `answer_by <= now` become `expired` (one precondition write
   each, `purge_after` set).
2. **Triage due:** for each user with `received` submissions whose triage interval has elapsed, one
   `memory_inbox_triage` task, dedup key `memory-inbox-triage:<user>:<owner-local date>`.
3. **Final backstop:** for each user whose `final_due_at` passed more than `FINAL_BACKSTOP_GRACE` (1 hour) ago and who
   holds `confirmed` candidates, one `memory_inbox_final` task (a lost enqueue, a failed pass).

Why not inside the janitor's job (`fact_lifecycle_sweep`, SCHEDULERS.md `:252-283`): the janitor has no retry by design,
a run guard, a log-only mode and a 500 on any failure; folding the inbox into it would let a janitor failure skip
triage and would make the janitor read a store that is not about fact expiry. One more Scheduler line is cheaper than
that coupling.

### 10.2 Triage: at most once a day, only when something arrived

- **The interval** is a new per-user setting `memory_inbox_triage_interval_days` on `UserBotConfig`, default 1, resolved
  user → account defaults → system default and counted in owner-local calendar days, exactly as
  `fact_review_interval_days` (`src/domain/user.py:258`; `review_interval`, `interval_elapsed`,
  `src/domain/fact_review_rules.py:406-420`). A daily run a few minutes later than yesterday's is never skipped.
- **Delta 2026-11-25, owner: frequency choice in the Cabinet.** The owner picks the interval (1, 2, 3 or 7 days) in the
  Settings → Connections row (§8.2); the sweep reads it unchanged and a write takes effect at its next run.
- **Only when something arrived:** the sweep enqueues only for users with `received` submissions.
- **The run** (new `MemoryInboxService`, `src/services/`): rechecks the interval (a redelivered or hand-sent task), takes
  the account's memory-write lease (`lease_until`, the run timeout; `src/domain/memory_write_lease.py`) — the account
  from the user's profile only, as `ConsolidationService` does — runs the passes, persists the decisions, sets
  `last_triage_at`, posts the notice (§11), releases the lease in a `finally`.
- **Lease busy → defer**, copying `FactReviewService._defer` (`fact_review_service.py:373-381`): one task delayed by
  `LEASE_BUSY_RETRY_DELAY_S` (`memory_write_lease.py:6`), dedup key `memory-inbox-triage-defer:<account>:<defer_bucket>`
  (`:19`), so losers in a bucket share it.
- **A failed pass** sets `last_triage_failed_at`, not `last_triage_at`, answers 200 `failed` with no Cloud Tasks retry;
  the next day's sweep enqueues again. Decisions already recorded stand.

### 10.3 Final: one hour after the last confirmation, one run per burst

- **Each "True"** writes `final_due_at = confirmed_at + FINAL_DELAY` (1 hour) on the state document in the confirm
  transaction, then enqueues one `memory_inbox_final` task delayed by `FINAL_DELAY`, dedup key
  `memory-inbox-final:<user>:<final_due_at epoch seconds>` (unique per confirmation).
- **Each task** reads the state first. `now < final_due_at`: a later confirmation moved the run; it answers 200
  `not_due` and does nothing. So a burst of N confirmations enqueues N tasks and only the last runs. This is the
  `external_task_timer` pattern (a stale timer is a no-op, SCHEDULERS.md `:318-322`).
- **A due task** takes the lease (busy → defer, as §10.2, key `memory-inbox-final-defer:<account>:<bucket>`), runs the
  final pass over every `confirmed` candidate, sets `last_final_at`, and clears `final_due_at` only if it is still the
  value it read: a confirmation made during the run keeps its own pending task.
- **A failed pass** re-enqueues the final task once more after `FINAL_DELAY`, bounded by the state's `final_attempts`;
  past it the sweep's backstop takes over the next day.
- Rejections set no final pass.

All three task types are `/worker` tasks: runners in `src/composition/worker_setup.py::build_worker_tasks` (`:40`), one
registry line each, rows in the SCHEDULERS.md map. Every task payload names a user, so `/worker`'s locked-user check
covers them (`ENTRY_POINTS["worker"]`).

## 11. Notification

After a triage that made N > 0 new candidates, one line through
`UserNotificationService.notify_ui_notice` (`src/services/user_notification_service.py:264-273`): a fixed UI string in
the owner's UI language, sent **literally**, to the primary channel else the last active one, and **not written to
history**. So the line never becomes a message of any role; `notify()` is not used, because it saves a system-alert
user turn to the session (`:4-9`).

The string (new `UIMessage.MEMORY_INBOX_CANDIDATES`, all four locales) carries `{count}` and `{source}`: the client name
when every new candidate came from one client, else a localized generic "your assistants". No candidate text. A failed
post is logged and not retried: the candidates wait in the Cabinet either way.

## 12. Observability

Log lines carry ids and counts, **never submission or candidate text, never a time field's text**:

- `[MemoryInbox] submit user=<8> client=<8> items=… stored=… cut=… over_day=… ms=…`, span `mcp.memory_inbox.submit`;
- `[MemoryInbox] triage user=<8> account=<8> submissions=… candidates=… discarded=… left=… passes=… status=…`;
- `[MemoryInbox] confirm|reject user=<8> candidate=<id> edited=0|1`;
- `[MemoryInbox] final user=<8> candidates=… wrote=… status=…`;
- `[MemoryInbox] sweep expired=… triage_enqueued=… final_backstop=…`.

The 📊 stats line and the 🚨 turn-cap alert gain the labels `inbox_triage` and `inbox` (memory README §3.11). The model
calls are recorded like every consolidation call: a `prompt_content` row per turn (which holds the text, as for every
pass) and Logfire spans.

## 13. Cost

No numbers here: none are measured. What drives it:

- **Triage:** at most one model run per user per interval, only when submissions arrived; up to `TRIAGE_PASSES` loops,
  each a large fixed prompt (the triage profile, the baseline, the shown lists) plus one `search_existing_facts` per
  thought (an embedding call each).
- **Final:** one run per confirmation burst; the conversation-row pass cost of the mail stage, over at most the
  candidates the owner confirmed; then the profile step's per-fact calls for the facts it wrote.
- **Zero model cost:** the tool, the sweep, the Cabinet, expiry, the notice.
- Billing as every agent call (`BaseAgent._call_llm`, memory README §8).

## 14. Rejected alternatives

- **Feed approved items through the `$memory save` session-pair path** (memory README §5.1). The pair reaches memory only
  at the channel's next overflow, not at the nearest run (median hours, days in a quiet channel, memory README §7); and
  the consolidator would read checked text as conversation, mixing it with the chat around it. It also puts external
  text into a user-role message, which decision 2 forbids.
- **No triage: raw items straight to the owner.** The owner expects heavy noise; all of it would land on the owner, and the
  Cabinet would show what memory already knows.
- **Ask the external model to read memory before it submits.** Triage has the full search and dedups better; an extra
  call per submission adds latency and pulls more facts into the model's context, which is more temptation to retell.
  The loop guard is a sentence in both descriptions plus triage's discard of the known (§7.5).
- **A second scope on the facts resource** (§5.1): one scope per resource, no per-tool scope in FastMCP, claude.ai
  registers what the metadata advertises, and voice tokens verify there.
- **Batch approval.** The owner's check is the only gate; a "confirm all" turns it into a formality.
- **Date pickers on the card.** They force precision the owner does not have; free text keeps imprecision honest, and
  the final pass dates by §2.7.
- **Write facts at confirmation.** It bypasses consolidation as the single curator (SCD2, dedup, conflict rules), as
  `decisions/memory_facts_owner_confirmed.md` already rejected.
- **Triage replaces a waiting candidate with an updated one** when a new submission refines or corrects it (owner,
  2026-11-25). Rejected: the date of a statement ranks it, and two statements on two days are both true at their time;
  merging them on the card hides the change the final pass must see. Two cards, each dated, and the final pass
  reconciles them (§7.4, §9.2).
- **`wrap_untrusted` with a nonce in the triage message.** It is built for file text and its store kinds; the JSON form
  of the mail stage keeps each submission inside its object, and the owner's check is the gate (§3).

**Relation to an earlier rejection.** `decisions/memory_facts_owner_confirmed.md` rejected "a pending store with an
hourly flush" as a second write path into consolidation, because the history pair already served the bot's own
proposals. Here the history pair is unavailable (external text never enters a session), so the inbox is that second
path by owner decision; the new decision record says so.

## 15. Open for the builder

1. **The triage profile's token set.** At least `CONSOLIDATION_TAXONOMY` (draft classification, expiry kinds, trivial
   exclusions); whether `CONSOLIDATION_POLICIES` and `CONSOLIDATION_OUTPUT_SPEC` belong too is settled by reading those
   tokens against a pass that writes no fact.
2. **The emitted `items` schema.** pydantic may emit `$defs`/`$ref` for a nested item model; clients handle a flat inline
   object best. Pin the shape against `list_tools()` and flatten if needed.
3. **The loop's "ended with a report" signal** for §9.4: `_run_consolidation_loop` returns `[]` both for a turn-cap and
   for a report with no operations; the `report` argument (`:1761`) can carry it.
4. **A final-pass dry run before deploy**, as `MEMORY_FACT_CONFIRMATION_RFC.md` §8 measured its echo trap: synthetic
   confirmed candidates through the real profile with writes intercepted, counting how many are captured. The
   `Domain_Scope` fallback "when in doubt, DISCARD" is the risk.
5. **The consent-page wording** for an inbox grant.
6. **`SAID_WHEN_LOOKBACK`** (365 days) and the §5.4 / §6.3 numbers are proposals, to be confirmed when built.
7. Whether the janitor's overdue-`purge_after` 🚨 (memory README §5.5 step 8) also watches `memory_inbox`.
8. Whether the Cabinet Home's Memory tile (`CABINET_REDESIGN_RFC.md`) shows the waiting count.

## 16. Components

| Layer | File | Content |
|---|---|---|
| domain | `memory_inbox.py` (new) | `Submission`, `Candidate`, statuses, `Basis` and its liberal mapping, the item normaliser, limits, retention |
| domain | `memory_inbox_contract.py` (new) | tool name, text keys, result keys |
| domain | `mcp_resource.py`, `pass_actor.py`, `ui_messages.py`, `locales/*` | resource and path; `inbox_triage`, `inbox` labels; the notice string |
| domain | `user.py` | `memory_inbox_triage_interval_days` |
| ports | `memory_inbox_repository.py` (new) | submissions, candidates, state; precondition writes |
| adapters | `firestore_memory_inbox_repository.py` (new) | the two collections, `purge_after` |
| config | `environment.py` | `memory_inbox_collection`, `memory_inbox_state_collection` |
| services | `memory_inbox_service.py` (new) | submit, list, confirm, reject, triage run, final run, sweep; lease and deferral |
| infrastructure | `memory_inbox_triage_scope.py` (new) | the two triage tools' validation and decisions |
| agents | `consolidation_agent.py` | tasks `triage_memory_inbox`, `consolidate_memory_inbox`; pass kind `triage` |
| composition | inbox MCP app (new), `mcp_setup.py`, `worker_setup.py`, `memory_setup.py` | resource, scope, tool; three task runners; wiring |
| web | inbox Cabinet blueprint (new), `mcp_dispatcher.py`, `mcp_consent_app.py`, `cabinet-memory.js` | routes, path, consent wording, the card |
| config | `config/firestore.indexes.json` | §6.4 |
| tokens | `MCP_MEMORY_INBOX_TOOL_TEXTS`, `MCP_MEMORY_INBOX_RESULT_TEXTS`, `CONSOLIDATION_INBOX_TRIAGE`, `CONSOLIDATION_INBOX_CANDIDATES`; profiles `consolidation_inbox_triage`, `consolidation_inbox` | Firestore, mirrored in `prompts_snapshot/` |

New behaviour gets new tests (the normaliser's coercion table, the triage scope's refusals, the state rhythm, the
confirm transaction, the emitted schema, the locked-user row); no existing test is edited to fit (root `CLAUDE.md`
reviewer rule).

## 17. Docs that change at land

- `docs/05_building_blocks/memory/README.md`: §1.1 (a second owner-confirmed writer), §1.2 (two collections), a new
  §5 subsection for the inbox, §4.7 (the no-retell sentence), §6 rows, §7 timeline, §8 cost, §3.11 labels; its
  `covers:` header gains the new files.
- `docs/05_building_blocks/remote_mcp_server/README.md`: the fourth resource, its scope (registrable, never default),
  routing, consent wording, the caching note.
- `docs/07_deployment/SCHEDULERS.md`: the job, the three task types, their dedup keys and deadlines.
- A new decision record in `decisions/`: the inbox decision, the rejected alternatives of §14, and the relation to
  `memory_facts_owner_confirmed.md`.
- Root `CLAUDE.md`: the trap "only an owner-confirmed `$memory save` path writes" names the inbox's final pass too; the
  "Where things live" memory row mentions external submissions.
- `src/utils/capabilities.py`: what an outside assistant can send and that the owner checks it.

## Built (differences from the text above)

- **Test store:** §6.5's in-memory fake is not built; tests run the real `FirestoreMemoryInboxRepository` on the evaluating
  Firestore fake (one implementation, index refusal for free).
- **Item normaliser:** its own module, `src/domain/memory_inbox_items.py`, not part of `memory_inbox.py` (§16).
- **Notice:** two strings, `MEMORY_INBOX_CANDIDATES_FROM` (`{count}`, `{source}`) and `MEMORY_INBOX_CANDIDATES` (generic
  wording), because `notify_ui_notice` localizes one key and formats fields as given (§11).
- **Final-pass order:** by the candidate's newest source receipt time, oldest first, not by `said_when` (free text the owner
  may have edited); every object carries `said_when` and the token tells the model to rank by it (§9.1).
- **Task deadline:** `INBOX_DEADLINE_S` 1800 for the triage and final tasks, the review's value (§10).
- **§15.7:** no coupling of the janitor to the inbox store; nothing watches the inbox's overdue `purge_after` yet.
- **§15.8:** no count on the Cabinet Home tile; the chat notice covers it.
