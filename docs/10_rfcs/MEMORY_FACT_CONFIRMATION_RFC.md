# RFC: Memory facts the owner confirms — `propose_memory_facts` + `$memory save`

**Status:** Accepted (owner, 2026-10-19). Replaces the `save_to_memory` intent.
**Related:** `AGENT_SKILLS_RFC.md` (the draft + `$skill save` pattern this copies),
`AGENT_WORKSPACE_RFC.md` (task state and the bot's own notes), `STANDING_DIRECTIVES_RFC.md`,
`SYSTEM_TURN_FINGERPRINT_RFC.md` (branch `rfc/system-fingerprint`).

## 1. Problem

`save_to_memory` is a zero-LLM intent (`memory_search_agent.py:163-186`). It puts the text into
`history_context["consolidation_text"]`, which `ConversationHandler` attaches to the user message of the turn
(`conversation_handler.py:1009-1017`). The consolidation serializer emits it **as a plain user part**
(`domain/consolidation_serialization.py`), so the consolidator reads bot-written text as the owner's own words.

Measured over 30 days of `prompt_content` (90 calls in 34 Smart traces):

| What was saved | Calls |
|---|---|
| Facts about the owner's life | 66 |
| Procedures and instructions for the bot | 14 |
| Notes about files | 8 |
| Task state | 2 |

- **Procedures became rules.** A saved flight-tracking procedure was consolidated into 4 `AGENT_DIRECTIVE` facts, so it
  entered `standing_directives` on every request. The owner's memory fills with irrelevant directives.
- **"Remember" means every store.** 32 of 34 traces were owner-requested: "remember" is the owner's word for every
  store, and the bot had one place to put it.
- **The bot cannot see its own save.** `consolidation_text` is never rendered to a model. It re-saved after an empty
  `search_memory` and once denied having saved at all.
- **Consolidation is late.** It runs when the window overflows: median 6.4 h, longest seen over 30 h.
- **Saves are lost off chat.** On reminder, daily-review and voice paths nothing stores `consolidation_text`.
  13 calls went this way; about one reached consolidation, and the bot was told `saved: True` every time.

## 2. Decision

The bot never writes to memory itself. It **proposes** facts and the owner **confirms** them, the same way skills are
saved (`AGENT_SKILLS_RFC.md`, delivery B).

1. **The owner asks.** "Here is my scan, remember the data."
2. **The bot proposes.** Smart calls the local tool `propose_memory_facts` with `facts: [{text, source}]`. The tool
   is declared **only on interactive turns** (`message.context["interactive_delivery"]`, set only by
   `ConversationHandler`), so reminder runs, the daily review, `ask_alek`/`tell_alek` and `/worker` cannot see it.
3. **The draft is stored.** `MemoryFactService.propose` validates the facts and stores an immutable draft under a
   short code.
4. **The owner sees the preview.** `ConversationHandler` posts the numbered facts with their sources, then the
   command `$memory save <code>` as a separate message. If the preview fails to post, the command is not sent.
5. **The owner pastes the command.** `ConversationHandler` handles it before any LLM call and takes the draft. It
   appends one history pair to the channel's session:
   - user: `[System: the owner confirmed these facts about their life for long-term memory. They reach memory after
     background consolidation; search_memory cannot find them before that.]` followed by one line per fact,
     `- <text> (source: <source>)`;
   - model: `Submitted N confirmed facts to memory.`

   The draft is deleted only after the pair is stored.
6. **Consolidation picks it up.** The pair leaves the window with the next overflow batch, like any history.

This solves each problem:

- **Provenance is true by construction.** The facts first appear in history in a user message the owner caused by
  pasting the code. The consolidator reading them as the owner's statement is correct, and no taxonomy change is
  needed.
- **The echo trap does not fire.** The consolidator discards facts that the ASSISTANT stated and the user only
  confirmed (`COGNITIVE_PROCESS_CONSOLIDATION`, rule "NEVER process ASSISTANT statements as facts…"). So the facts
  must not appear in a model message first:
  - the preview is a delivery, not history;
  - the tool result tells the bot not to list the facts again in its reply.
- **Visibility.** The pair stays in the window until it is consolidated. Its frame tells the bot that `search_memory`
  cannot find the facts yet, so "saved but not found" is answered from history.
- **Off-chat paths cannot write** (the tool is not declared there), so nothing is lost behind a false `saved`.
- **An injected document cannot write either.** A drive file with a thousand fake facts gets a long preview, and
  nothing reaches memory unless the owner pastes the code.
- **The owner's own words are not proposed.** Consolidation already reads the owner's messages; proposing them would
  duplicate them. The tool description says so.

## 3. Tool contract

`propose_memory_facts`, a Smart local tool served by `DelegationEngine(local_tools=)` like `draft_skill`.

**Description** (in git, `infrastructure/memory_fact_tools.py`):

> Propose facts about the user's life for long-term memory; the owner confirms them. Memory is filled by background
> consolidation of this conversation: the user's own messages are picked up automatically — never propose what the
> user said themselves, including "remember that I…" — and your replies only as short summaries. Facts you only read —
> a file, a drive document, an email, a search result — are not picked up. When the user asks you to remember facts
> from such a source, propose them here. The owner sees the list with a save command; nothing is stored until they
> paste it, and then the facts reach memory only after consolidation, usually hours later; search_memory cannot find
> them before that. Only facts about the user and their life: people, health, home, documents, dates, possessions,
> decisions. Never procedures or how-tos (propose a skill), rules for your own behaviour, task progress, or facts about
> the world.

**Parameters:** `facts` is an array of 1 to `MAX_FACTS_PER_DRAFT` objects:

- `text`: one self-contained fact in the third person ("User …"), with its date when known. One line, at most
  `MAX_FACT_CHARS`.
- `source`: where it comes from (file name, drive path, email subject or URL). One line, at most `MAX_SOURCE_CHARS`.

**Result** (no code in it, as with `draft_skill`): `SYSTEM: The owner sees N proposed facts and a save command.
Nothing is stored yet. Say in one sentence that pasting the command saves them; do not list the facts again.`

A validation error, a security refusal or a store failure is a failed tool result that names the problem, in the
`draft_skill` style.

## 4. Limits and checks

| Limit | Value | Why |
|---|---|---|
| `MAX_FACTS_PER_DRAFT` | 30 | One reviewable preview. More is split over drafts, so the owner reads each one. |
| `MAX_FACT_CHARS` | 300 | About 40 words, the consolidator's own fact size. |
| `MAX_SOURCE_CHARS` | 150 | One label. |
| Draft TTL | 7 days | `expires_at` with a Firestore TTL policy (owner step), plus an expiry check on read like skill drafts. |
| Code | 4 hex characters, unique per user, retried on collision | Same as skill codes. |

- **Security check.** Every fact and source passes `SecurityPort` in the UNTRUSTED zone at propose time, and a
  flagged draft is refused, as `SkillService._check_text` does. They pass again at confirm time, because the text
  enters every later consolidation prompt.
- **Validation** is pure domain: non-empty, one line, length caps, count caps.

## 5. Components

| Layer | File | Content |
|---|---|---|
| domain | `memory_fact.py` (new) | `ProposedFact`, `MemoryFactDraft` (pydantic, validators), limits, `save_command(code)`, `render_confirmed_note(facts)`, `MEMORY_PREVIEW_DELIVERY` |
| domain | `exceptions.py` | `MemoryFactsRejected`, `MemoryDraftNotFound` |
| ports | `memory_fact_draft_repository.py` (new) | `create_draft(user_id, code, draft) -> bool` (create-if-absent), `get_draft(user_id, code)`, `delete_draft(user_id, code)` |
| adapters | `firestore_memory_fact_draft_repository.py` (new) | `{prefix}memory_fact_drafts/{user_id}:{code}`, `expires_at`, expiry on read |
| config | `environment.py` | `memory_fact_drafts_collection` |
| services | `memory_fact_service.py` (new) | `propose(user_id, facts) -> (code, draft)`, `get_for_save(user_id, code)`, `finish_save(user_id, code)`; security checks; `[MemoryFacts]` log line with counts only |
| infrastructure | `memory_fact_tools.py` (new) | Declaration, `make_propose_memory_facts_handler(propose)` |
| agents | `core/smart_response_agent.py` | Constructor `memory_fact_service`; declare and serve the tool on interactive turns, **outside** `if skills:` (`smart_response_agent.py:482`) |
| handlers | `conversation_handler.py` | Deliver the preview; `$memory save CODE` and bare `$memory` (usage); append the pair; localized replies |
| locales | `uk/en/fr/es.py` | Preview header, saved, not found, rejected, unavailable, usage, delivery failed |
| composition | `service_container.py`, `user_agent_factory.py` | Wire the repository and service; pass the service to Smart and `ConversationHandler` |
| user-facing | `utils/capabilities.py` | "Ask me to remember facts from a document; you confirm them with a save command" |

The port is justified: it is a system boundary (Firestore) with an in-memory fake for service and handler tests,
the same as `SkillRepository`. The service takes the port and `SecurityPort`, and imports no other service
(REQ-ARCH-22).

## 6. What is removed

- **The intent.** `Intent.SAVE_TO_MEMORY`, its capability, description and context schema in `agent_manifest.py`;
  `FactsMemoryAgent._handle_save` and its routing. The `text` field stays: `save_file_to_drive` owns it.
- **The writers of `consolidation_text`:**
  - `ConversationHandler`'s attach (`:1009-1017`, `:1374-1382`);
  - the long-turn late-answer attach (`long_turn_service.py:102-103`);
  - the `consolidation_text` key in the history-context plumbing.
- **Manifest prose.** The drive text "facts go to save_to_memory" (`agent_manifest.py:573`) now points at
  `propose_memory_facts`.

**Kept on purpose (explicit deferral).** The `MessagePart.consolidation_text` field and the serializer's
`p.consolidation_text or p.text` read stay. Saves made before deploy still sit in live windows (12 pending on
2026-10-19), and removing the read would drop them. A follow-up removes both once a check shows that no stored
session message carries the field.

No alias for the old intent name. A model that still emits `save_to_memory` gets the coordinator's "No agent
registered for intent" failure, and its tool list shows `propose_memory_facts`.

## 7. Owner steps (Firestore, not in git)

1. **`PROTOCOL_AGENT_SELECTION`:**
   - delete the `save_to_memory { … }` block, including its "Do not compress… lose nothing" rule, which works against
     short facts. The tool description now carries the guidance;
   - in the drive `untrusted` line, change "save to memory" to "propose memory facts".

   Back up the token first, as before.
2. **TTL policy** on the collection group `{prefix}memory_fact_drafts`, field `expires_at`
   (`docs/07_deployment/README.md` gets the command).

## 8. Tests and verification

New behaviour gets new tests. An existing test broken by the removal goes to a reviewer (CLAUDE.md reviewer rule).
Known ones:

- the `TestSaveToMemory` class;
- manifest drive tests naming the intent;
- `test_drive_eval_g3_injection.py`, whose injected-case bar must now name `propose_memory_facts`;
- the long-turn and session-store `consolidation_text` writer tests.

New tests:

- **Domain:** validators (empty, multi-line, length, count), `render_confirmed_note`, `save_command`.
- **Adapter wire tests** against a stubbed Firestore client: create-if-absent, expiry on read, delete.
- **In-memory fake of the port**, with a contract test.
- **Service:** propose (codes, collision retry, security refusal), confirm path, expired and unknown code.
- **Tool handler:**
  - validation errors as failed results;
  - the delivery item;
  - the result does not contain the code or the facts.
- **Smart:**
  - the tool is declared only with `interactive_delivery`, and also when the skill store fails;
  - it is absent on a notify-path message.
- **ConversationHandler:**
  - preview order (list, then command; no command if the list failed);
  - `$memory save` appends the exact pair, then deletes the draft;
  - an append failure keeps the draft and replies with an error;
  - an unknown code; bare `$memory`;
  - a backtick-wrapped command still dispatches (existing adapter stripping).
- **No writer left:** `consolidation_text` is set nowhere in `src/` except the legacy serializer read.
- `make check`.

**Consolidation dry run** (`scripts/consolidation/test_stage1_classification_dryrun.py`, `--runs 3`): a batch with a
confirmed-facts pair must yield those facts as user facts and no `AGENT_DIRECTIVE`. A pair whose facts also appear in
the preceding model reply (the case the tool result forbids) is run as the negative control for the echo trap.

**Live acceptance:**

1. Ask to remember data from an uploaded document. Check the preview and the command, paste the command, and check
   the pair in the session.
2. A reminder run has no `propose_memory_facts` in its Logfire tool list.
3. "Remember that I…" is answered without a proposal.
4. After the next overflow, the facts are in the consolidation batch and in memory.

## 9. Alternatives rejected

- **Keep `save_to_memory` and label its block for the consolidator** (assistant-submitted, source). The bot still
  decides what goes in, and procedures still go in. The owner asked for "only on my direct request", and only a
  confirmation step enforces that in code.
- **Quotas without confirmation.** They bound volume but not content: a quota of 20 fake facts still writes 20.
- **A pending-facts store with an hourly flush** (brainstorm option C). Faster, but a second write path into
  consolidation and more Stage 2b runs, for a wait the history pair already makes visible.
- **Write facts directly, bypassing consolidation** (option D). It breaks consolidation as the single curator, SCD2
  and deduplication.
- **Detect "remember" in the owner's text.** Brittle across uk/ru/en/es, and still the bot decides the content.
