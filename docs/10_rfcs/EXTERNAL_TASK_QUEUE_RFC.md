# RFC: External task queue

**Status:** Accepted (2026-11-23); steps 1–4 implemented, step 5 pending.
**Owner's map:** `docs/05_building_blocks/external_task_queue/README.md`.
The decisions are in §2. Every example below is invented: the user is Alex Doe, the worker labels are `research` and
`calendar-watch`, and the tasks are "compare three community-garden irrigation kits for Shelbyville" and "find what
changed in the Example School term calendar".

**Decision of 2026-11-24 (supersedes the taint and hold rules).** The owner dropped taint tracking for the queue: every
task created in a chat turn waits for the owner's confirmation with its exact frozen text (the `$memory save` shape), a
task created while one of the owner's reminders runs goes straight to the queue, completion reaches the bot as a system
alert, and task creation moves to a specialist agent. Record:
`docs/04_solution_strategy/decisions/external_task_confirmation_and_alerts.md`. Sections marked "superseded" below
(§5.3 taint and the tainted create, §5.9's taint of reads, §6.1's taint mitigations, D9's "outside content" hold) stay
as the history of the first design until the code that implements them is removed.

**Amendment of 2026-11-24 (the task's note).** Every task carries the bot's own return instructions, shown to the owner
in the receipt (and, for a reminder-run task, in a show-only notice) and handed back to Smart by the completion alert as
its own instructions; no worker surface carries it, and it never enters saved history. Where §5.9 and §6.1 describe the
alert as carrying only code-made values, the alert of a task with a note also carries that note. Record: the
amendment in `docs/04_solution_strategy/decisions/external_task_confirmation_and_alerts.md`.

**Implementation note (the task specialist, D14, 2026-11-24).** §5.3's tools are built as a specialist's: Smart has no
queue tool and offers one internal intent, `work_with_external_tasks`, on the owner's own chat turn of a flagged user.
`ExternalTaskAgent` (`src/agents/external_task_agent.py`) serves the five tools, repeats the gate in code, forwards the
brief word for word (enforced in code) and composes its answer from every tool result. Record:
`docs/04_solution_strategy/decisions/external_task_specialist.md`; the built flow: the owner's map.

**Implementation notes (step 1).** The build differs from the body below in two ways; the body is kept as specified.
Smart has five tools, not four: `list_external_workers` (the owner's registered labels) was added and
is on the taint allowlist. The taint rule has more sources and markers than §5.3 lists: the final flag is saved on the
model message (`execution_tainted`), so later turns that load it are tainted; a forwarded Telegram message taints; a
listing that shows a task created tainted (`created_tainted`) taints; reminders written in a tainted execution are
flagged and wrapped wherever they reach a model; and every workspace write in a tainted execution carries the
`tainted_execution` mark. The built rule as a whole: `docs/05_building_blocks/untrusted_content_taint/README.md`.

**Implementation notes (step 2).** The build differs from §5.6 and §5.8 as follows; the body is kept as specified.
- `wake_generation` moves on in its own worker transaction right after the task write, not inside it: the task and
  the worker sit behind two stores. A crash between the two loses one ping, which §7 already tolerates.
- "The first `queued` task" is read as "the worker's oldest queued task": of two concurrent creates the older pings,
  and a confirmed held task that is older than waiting ones pings once more (harmless). A lease timer that re-queues a
  task pings whether or not other tasks wait. A submit, a fail or a release by the worker pings for its oldest queued
  task, so a worker that takes one task per run is called back for the rest.
- The wake body is `{user_id, account_id, worker_id, generation}`; the ping names the worker by its `worker_id`.
- `WakeRefusal` has two more codes, `server_error` and `unreachable`, the retryable ones; they never reach the owner. A
  name that does not resolve is `dns_failure` (final); a send that outruns `WAKE_TIMEOUT_S`, DNS included, is
  `unreachable`. The wake task bounds its own deliveries (`WAKE_MAX_ATTEMPTS`, read from the retry-count header),
  because the queue it shares has one retry setting for every task type.
- The Cabinet enters the wake-up target as a pasted `curl` command plus a secret (`src/domain/wake_curl.py`, new save-time `WakeRefusal` codes `curl_*`); the stored `{url, headers}` is unchanged.
- Stricter than specified: `.localhost`, `.local` and `.arpa` are refused like `.internal`; `Content-Type`, `Keep-Alive`,
  `TE`, `Trailer`, `Upgrade` and `Proxy-Connection` like the framing headers. The own-host list is every deploy URL the
  main service is configured with (its URL, both OAuth callbacks, the MCP resource, the relay, the indexer, both
  sandboxes); there is no separate custom-domain key, so a custom domain counts when it is one of those URLs.
- The Cabinet shows the target's host only, not the header names (§5.1 keeps only the host in clear).
- The refusal notice is a literal post to the origin channel of the worker's oldest queued task, with no history pair.
- A worker's description is never shown to Smart (§5.1 said it is): it is free text typed in the Cabinet, and a tool
  or parameter description is read with the schema's authority. Smart's tools carry labels only.

**Implementation notes (step 3).** The build differs from §5.4 and §5.11 as follows; the body is kept as specified.
- A worker key that `resolve_key` or the whitelist refuses on `/tasks-mcp` is a `401` at the door, not a tool error:
  the SDK's verifier returns a token or nothing. REST answers `403` for a locked, switched-off, revoked or
  not-whitelisted key. The lock text still answers a lock that happens between the verify and the call.
- The rate limits are counted per worker (`user:worker`) and shared with REST, in REST's buckets, not per key or per
  grant. The `401` address limits of REST are not applied on MCP.
- `TokenTextFastMCP` takes a read-only tool set: only `list_tasks` carries `readOnlyHint`; the five tools that write
  carry no annotations.
- The whitelist rule moved out of the REST route into `ExternalWorkerUserGate`, which both surfaces call.
- The task text in a claim result is plain: the owner's brief, never wrapped. Every refusal line names its code
  (`code: stale_lease`); `not_enabled` and `worker_revoked` share one neutral line with no code. A failure other than
  a refusal answers a generic tool error naming only the exception type.
- `external_tasks` is not in `SCOPE_OF` yet and no registration can carry it: that is step 4.

**Implementation notes (step 4).** The build differs from §5.4 "OAuth" and §5.6 as follows; the body is kept as
specified.
- Refresh tokens also record `granted_at` (the code exchange, copied through every rotation) and `issued_at` (their
  own issue): the Cabinet shows them as "granted" and "last refresh". Older tokens read both as null.
- The authorization service serves the tasks resource when its `scope_of` holds the tasks scope (composition's
  `SCOPE_OF` does), with the URI derived from the facts URI as the drive URI is; there is no separate constructor
  argument, because the constructor is at the arity cap. Without the scope the tasks URI and scope are refused.
- Removing a worker revokes its grants through the Cabinet route, which calls the queue's `revoke_worker` and then the
  authorization service's `revoke_worker_grants`: services never call each other. A failed grant revocation is
  finished by a retry of the removal, which is safe to repeat.
- The flag check at code exchange also refuses a tasks code that carries no worker.
- Per call, a refused grant answers the neutral refused line (`isError`) that `not_enabled` and `worker_revoked` share;
  the limits are the worker's buckets for a key and an OAuth grant alike (step 3's note).
- `main.py` builds the MCP components before the Cabinet blueprint, so the Cabinet gets the authorization service; a
  failed build still leaves the bot up without MCP.
- §10 Q1's remaining live check (the authorize-time scope equals the registered one) is run after the deploy.

## 1. Purpose

Some work is better done by an agent or a service the user already runs elsewhere: a long piece of research, or a job
that needs tools or models the bot does not have. Today the bot can only do such work itself or not at all.

This RFC adds a **per-user queue of external tasks**. In a chat turn, the bot (Smart) hands a free-text task to "an
external worker". A worker is anything outside the bot that the user runs: an MCP client, a scheduled agent, a script,
an automation service. The worker claims the task, does it elsewhere and submits a text result. The bot stores the
result as untrusted content and tells the user where to find it. Smart reads the result only when the user asks.

The queue does not know what a task is about. A skill of the user decides when Smart enqueues and which worker gets the
task. A new kind of hand-off is a new skill, not new code.

## 2. Decisions (2026-11-23)

Owner decisions. This RFC specifies them; it does not argue them again.

- **D1 One generic queue.** There is one queue, not one feature per use case. The user's own skills decide *when* Smart
  enqueues and for which worker (custom skills, `docs/05_building_blocks/agent_skills_workspace/README.md`). Code does
  not decide this. A new kind of task is a new skill and needs no code change.
- **D2 Two worker surfaces over one service.** (a) A new remote MCP protected resource with its own scope. (b) A plain
  HTTP (REST/JSON) endpoint for clients that are not AI. Both are thin handlers over the same queue service and hold no
  logic of their own.
- **D3 Workers are registered per user in the Cabinet.** Each worker has a label (which tasks it takes) and, optionally,
  a wake-up URL with secret headers. Those secrets are encrypted at rest. A user-entered URL needs an outbound-request
  guard (https only, public addresses only, no redirects), which the repo does not have yet; this RFC specifies it (§5.8).
- **D4 Wake-up.** When a task is enqueued for a label whose worker has a wake-up URL, the bot POSTs to that URL. The
  POST carries no task content: at most an opaque id. It is sent by a Cloud Task with retries, never from inside the
  chat turn. There is no ping while that worker holds a live lease, because the worker drains the queue in one session.
  When a lease expires and work is left, the bot pings again; so does a worker's submit, fail or release while others
  of its tasks are queued (amended 2026-11-25: a worker may take one task per run). Workers without a URL poll.
- **D5 Lease semantics.** A claim returns one task with a lease expiry. When a lease expires, the task goes back to the
  queue. Submit is idempotent by task id. A worker can also release a task, or fail it, explicitly.
- **D6 The result reaches the user as a bot notification and is untrusted content.** It never enters the session as a
  user-role message, because the consolidator reads every user-role message as the owner's own words (root `CLAUDE.md`,
  "The bot never writes to memory itself").
- **D7 Per-user enablement flag.** The feature is switched on per user, as a rollout and exposure control. A user
  without the flag gets no Smart tool, no MCP scope and no REST access.
- **D8 Non-interactive worker auth.** An AI client such as claude.ai custom connectors uses the existing OAuth flow. A
  script or CLI uses a bearer key instead: one key per worker, minted in the Cabinet, shown once, stored as a digest,
  revocable, bound to the user and the worker, and accepted on both the REST endpoint and the MCP resource.
- **D9 Security comes first.** Nothing a worker returns may bring anything harmful into the system. Every worker, and
  every leaked worker key, is treated as potentially hostile:
  - Results are data. They are never instructions and never actions.
  - Enqueueing happens only in a turn where the user is present. A task that outside content may have shaped is held
    until the user confirms it (§5.3).
  - Results are text only in the first version.
  - Links in a result are never fetched automatically. Names that come from a worker are sanitised, and a worker never
    chooses where its result is stored.
  - A worker sees only the tasks of its own user and its own worker identity.
  - Calls are rate-limited per worker key, and task text, results and open tasks are capped.
- **D10 Result destination.** By default the result goes to the user's agent workspace. Optionally, a per-worker
  Cabinet setting sends it to the primary drive's Inbox instead (D13). The bot chooses the file name and the folder,
  never the worker.
- **D11 No voice hand-off.** Lelik cannot create or read external tasks. This is out of scope (§4).
- **D12 The flag is set only by an operator**, by a direct database write (a script or the console). It is never a
  Cabinet switch and never self-service (§5.10).
- **D13 The drive destination is the primary drive's Inbox only.** No other drive and no other folder (§5.9).
- **D14 The task specialist (2026-11-24).** Smart has no queue tool. The five tools belong to one LLM specialist,
  `ExternalTaskAgent` (intent `work_with_external_tasks`, internal), which Smart reaches with a natural-language
  instruction: the worker's name and the whole brief, or which task to read or cancel. The brief reaches the worker
  word for word, enforced in code. Smart's own surface is one short intent line. Why, and the alternatives:
  `docs/04_solution_strategy/decisions/external_task_specialist.md`.
- **D15 Task files (2026-11-26).** A task may carry files: the assistant names them by reference, the queue copies their
  original bytes when the task is created, the receipt lists them, and the worker fetches each one by a short-lived
  link it asks for while it holds the lease. The worker sees only what was attached, never where it came from, and
  nobody has to clean up: the storage deletes the copies by age (§5.12).

**Engineering decisions of the review round (2026-11-23).** These close the questions the first draft left open.

- **E1 Leases are renewable** by the worker that holds them, through a `renew` operation on both surfaces. Renewal uses
  the same transaction precondition as submit, and a task has a renewal cap (§5.2).
- **E2 Background runs do not read task status.** The Smart tools exist on interactive turns only (§5.3).
- **E3 Wake-up secrets are encrypted on the worker record** as one ciphertext bound to the record, not in the credential
  store (§5.6).
- **E4 A wake-up URL uses port 443 only** (§5.8).
- **E5 The history pair never carries the title** (§5.9).
- **E6 Worker keys do not expire.** They are revocable, and the Cabinet shows each key's last-used time (§5.6).
- **E7 The numbers are set at implementation.** The named constants of §5.11 stay.

The security review's fixes are folded into §5–§7 and are not listed again here.

## 3. Current state (verified in code, 2026-11-23)

**Remote MCP server** (`docs/05_building_blocks/remote_mcp_server/README.md`).
- There are two protected resources, `MCPResource.FACTS` and `.DRIVE` (`src/domain/mcp_resource.py:15-17`). Each has
  one scope, bound by `bind_resource` (`:46-78`). With `resource` given, every requested scope must belong to that
  resource. Without it, the scopes select the resource, and a mixed or unknown scope is refused (`:71-77`).
- The scope strings live in composition: `MCP_DEFAULT_SCOPE = "user_context"`, `DRIVE_SCOPE = "drive_read"` and
  `SCOPE_OF` (`src/composition/mcp_setup.py:53-57`). Both scopes are in `valid_scopes` *and* in `default_scopes`
  (`:107-111`), so a dynamic registration that names no scope is registered for both.
- `/authorize` binds resource and scope, then requires every bound scope to be registered for the client
  (`src/services/mcp_authorization_service.py:231-242`).
- One token carries one resource's scope. Consent therefore approves or denies that one bound scope. The page lists it
  and has only Approve and Deny (`src/web/mcp_consent_app.py:128-136`, `:193-208`).
- The consent page identifies the approving user from the live Cabinet session (`:53-62`).
- Dynamic registration accepts only redirect hosts in `_ALLOWED_REDIRECT_HOSTS`: the claude.ai hosts and loopback
  (`mcp_setup.py:63-68`, enforced at `mcp_authorization_service.py:539-549`). So the only OAuth clients that can register
  are claude.ai custom connectors and loopback tools.
- No grant is gated per user, only by user state: `_allow_reason` checks that the user exists, is active, has an email
  and is whitelisted (`mcp_authorization_service.py:510-522`).
- `grant_allowed(user_id, grant_id)` checks that some refresh token of the grant is still active, then applies
  `_allow_reason` (`:482-498`, through `has_active_grant`, `src/ports/mcp_client_repository.py:105`). Today only the
  drive connector's one-time link calls it on use.
- Routing is done by a plain ASGI dispatcher (`src/web/mcp_dispatcher.py:18-71`). The facts matcher is exact on `/mcp`
  and `/mcp/` (`:20-21`), so a new FastMCP path under `/mcp/` would shadow `/mcp/consent` (README:238-241). The drive
  paths are checked first because the facts metadata test is a prefix match (`:26-27`, `:36-40`). The lifespan enters
  each MCP app's session manager (`:50-62`).
- The tool's user always comes from the verified token, never from a tool argument. The drive verifier builds an
  `AlekAccessToken` with user and account (`src/composition/drive_mcp_setup.py:136-151`), and `_Caller.run` takes them
  from it (`:281-282`). It then refuses a locked account before any read with `ACCOUNT_LOCKED_TOOL_ERROR`
  (`:289-291`; text at `src/domain/mcp.py:27`), using `InactiveUserCheck`, which reads the stored user fresh
  (`src/services/inactive_user_check.py:18-26`).
- **Voice token** (`src/services/voice_mcp_token_service.py`). This second token kind is a short-lived HS256 JWT under
  its own key (`:104-109`). Verifiers route on the unverified header's `kid` (`:80-86`). The tenant comes from a
  server-side live-call record, not from the token alone (`:19-21`, `:136`). Mint re-reads the whitelist (`:129`).

**Locked-user entry points** (`tests/unit/test_locked_user_entry_points.py`).
- `ENTRY_POINTS` (`:41-55`) holds one row per entry point. `ROUTE_ROWS` (`:90-105`) and `NOT_A_USER_ENTRY` (`:58-87`)
  classify every route the main service serves. Every `/api/` route counts as behind the Cabinet gate
  (`CABINET_GATE_PREFIX`, `:106`).
- `MCP_PATHS` (`:110-111`) must equal the literals of `_is_mcp_path` plus `DRIVE_PATH` (`:145`). A new MCP path
  therefore changes that assertion.
- The holders whose check is optional are pinned by `test_the_entry_modules_hand_every_holder_its_check`
  (`:154-169`).

**Smart local tools.**
- Each tool is a declaration builder plus a handler factory built per execution: `use_skill`
  (`src/infrastructure/skill_tools.py:34-87`), `workspace` (`src/infrastructure/workspace_tools.py:58`, `:207`).
  They are wired in `src/agents/core/smart_response_agent.py:516-566`.
- The user id is closed over (`:433`, `prompt_user_id`) and is never a tool argument. Results say `Tool note:` /
  `Tool error:` (`skill_tools.py:66`).
- `draft_skill` and `propose_memory_facts` exist only when the context carries `interactive_delivery` (`:524-532`).
  Only the chat path sets that flag (`src/handlers/conversation_handler.py:924`).
- **`interactive_delivery` means the user is present in the chat. It does not mean the user asked for any particular
  tool call.** In the same turn Smart may read outside content (an email, a drive file, a web page) and act on it.
- The owner-confirmation pattern exists for exactly this case:
  - `draft_skill` (`skill_tools.py:174`) stores a draft and delivers the command `$skill save <code>`, which only the
    owner can send (`docs/05_building_blocks/agent_skills_workspace/README.md:108-111`).
  - `$memory save <code>` works the same way for facts.
  - `$` commands never reach a model or session history (`CABINET_DEVICE_SESSIONS_RFC.md` §4).
- A tool result can carry `history_context` (`src/infrastructure/delegation_engine.py:63`). Smart copies it into the
  persisted turn's metadata (`smart_response_agent.py:660-661`).
- A background run of Smart, such as a reminder fired through `notify`, still gets:
  - its delegation tool over every available intent (`_get_tool_declarations`, `:782-785`);
  - `use_skill` when the user has skills (`:518-523`);
  - `workspace` on every path (`:541-555`);
  - `cancel_long_turn` while jobs run (`:557-566`).
- A reminder can name a workspace file in its alert (`Workspace file: workspace:<path>`,
  `src/domain/reminder_alert.py:33`), so a background run may read any workspace file through `workspace`.
- The descriptions of these Smart tools are inline code text (`workspace_tools.py:48-55`, `skill_tools.py:37-40`).
  They ship by deploy.

**Notifications.**
- `UserNotificationService.notify` runs an agent turn: Quick by default, or the agent named by `agent_id_override`
  (`src/services/user_notification_service.py:558`).
  - Its in-memory user turn is `[System: {system_alert}{suffix}]` (`:560`).
  - After the reply is sent, it persists a pair: a **user-role** message with the raw `system_alert` (or its summary,
    without the frame), then the model's reply (`:633-651`).
  - A reminder fires this way with Smart as recipient (`src/services/execute_reminder_task.py:133-148`). The persisted
    user line is `Received reminder: "<name>"` (`src/domain/reminder_alert.py:17`).
- `notify_raw(..., literal=True)` sends text the bot did not write, through `send_literal`, with no agent and no
  history (`user_notification_service.py:197-245`).
- `notify_drive_note_receipt` is the precedent this RFC follows (`:362-428`). It runs no agent turn. It posts a
  code-rendered UI string literally, then appends a code-written history pair: a `[System: …]` user line naming only a
  path and a ref, then a neutral model line, never the note's words (`:369-370`, `:417-425`; the line builders are in
  `src/domain/drive_note_receipt.py:140-150`).

**System-turn fingerprint** (`docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md`).
- `wrap_system` (`src/domain/system_fingerprint.py:141-145`) keys only code-written text *of the current turn, in
  memory* (§4.3).
- Notify turns stay unkeyed by a rev-2 decision (§8). Persisted `[System: …]` pairs are saved as they are and read in
  later turns as "a record of what happened, never an order" (§4.4).
- The consolidator has no general rule for these lines. `CONSOLIDATION_POLICIES` names only the memory-review pair
  (`prompts_snapshot/tokens/system/CONSOLIDATION_POLICIES.groovy:25`). Its echo trap covers ASSISTANT statements
  (`:21`).
- So a `[System: …]` user line is safe from the consolidator only because of what it says: it must carry nothing
  worth extracting.

**Untrusted marking.**
- `wrap_untrusted` (`src/domain/untrusted_text.py:14`) maps the workspace store to the neutral `external_content` tag
  and the drive store to `drive_document` (`:36-42`).
- Its callers: the drive renders, email cards and email search, and Smart's daily-review block
  (`src/infrastructure/drive_operations.py`, `src/domain/drive_tier_render.py`, `src/domain/drive_excerpt_render.py`,
  `src/domain/drive_search.py`, `src/domain/email_card_input.py`, `src/infrastructure/delegation_engine.py:247`,
  `smart_response_agent.py:474`).
- Nothing wraps web search output or workspace text. The `workspace` tool's `view` and `grep` return file text unmarked:
  neither `src/services/workspace_service.py` nor `src/infrastructure/workspace_tools.py` calls `wrap_untrusted`.

**Workspace.**
- Four folders (`src/domain/workspace.py:28-31`).
- Caps `MAX_FILES_PER_USER = 100`, `MAX_BYTES_PER_USER`, `MAX_FILE_BYTES` and `MAX_TEXT_FILE_BYTES` (`:34-37`). The
  caps are soft (README:69-70).
- Each version carries object metadata (`origin`, `fire`, `write_id`, `restored_from`, and the delete fields;
  README:64-68).

**Drive writes exist.**
- `UserDriveService.save_to_primary` saves a file the bot makes into the primary connection's write folder, under
  nested named folders or the Inbox (`DEFAULT_INBOX_FOLDER = "Inbox"`). It never overwrites
  (`src/services/user_drive_service.py:435-450`, `src/domain/user_drive.py:25`).
- `can_save_to_primary` is the predicate (`:425-433`).
- Call text files already land in `Inbox/<voice calls folder>` this way
  (`docs/05_building_blocks/user_drive/README.md:158-174`).

**Cloud Tasks and `/worker`.**
- `enqueue_worker_task` always targets the service's own `/worker`, with OIDC (`src/adapters/gcp_task_queue.py:346-410`).
- A `dedup_key` names the task `{task_type}-<sha256[:32]>`, and `AlreadyExists` is a silent no-op (`:380-384`,
  `:405-410`).
- A name dedupes only for about an hour, so rhythm comes from stored state (`docs/07_deployment/SCHEDULERS.md`, "The
  hub"; `docs/04_solution_strategy/decisions/worker_task_dedup.md`).
- A new task type is a runner, one registry line and one row in the task-type map (SCHEDULERS.md, "Registry").
- `/worker` refuses a locked user for every task type whose payload names a user (`ENTRY_POINTS["worker"]`).

**Outbound HTTP and egress.**
- `httpx` lives in `adapters/` (`src/adapters/http_call_control_plane_adapter.py:4`, `:20-22`).
- The only outbound client with guard-like rules is `HttpRangeSource`: https only, `follow_redirects=False`,
  `trust_env=False` (`src/adapters/sandbox_client/media_range_source.py:10`, `:55-57`, `:67-70`). Its URLs come from
  drive providers, so it has no address check.
- No code resolves a host and refuses private, loopback or link-local addresses.
- The main service has no VPC egress: its deploy step sets no `--network` and no `--vpc-connector`
  (`cloudbuild-dev.yaml:26-51`). Only the sandbox services get a VPC network (`:372-373`, `:422-423`).

**Secrets at rest** (`docs/05_building_blocks/secrets_at_rest/README.md`).
- `FirestoreCredentialStore` is shaped for OAuth grants: an `OAuthCredentialsPort` whose single ciphertext is
  `{access_token, refresh_token, account_label}`.
- Its `save` refuses a record without a provider account subject, `integration` or `account_id`
  (`src/adapters/firestore_credential_store.py:1-22`).
- `keyed_digest` is the verify-only primitive (`src/domain/secret_digest.py:13-16`).

**Per-user flags.** Per-user switches are fields of `UserBotConfig` (for example `drive_copy_deep_research`,
`src/domain/user.py:340`). The agent factory caches the profile for up to an hour after last use
(`CABINET_DEVICE_SESSIONS_RFC.md` §4).

**Firestore conventions.**
- A queue claim is an atomic precondition write (`src/ports/agent_note_port.py:83-142`, used by
  `src/services/reminders_service.py:86`).
- Records meant to vanish carry `purge_after`, with a TTL policy listed in the runbook
  (`docs/07_deployment/README.md:374-385`) and pinned by `tests/unit/test_ttl_registry.py`.
- Every client passes `database=`, and every collection name comes from `EnvironmentConfig`.

**Cabinet.** The redesign puts integrations under Settings → Connections (`docs/10_rfcs/CABINET_REDESIGN_RFC.md` §4,
§5.1). Every Cabinet operation is pinned by the operations inventory test (§6a).

## 4. Goals and non-goals

**Goals.**
- One queue service. Smart's tools, the MCP resource and the REST endpoint are thin callers of it.
- A worker learns of work by a content-free ping or by polling, takes one task at a time under a lease, and submits a
  text result.
- The result reaches the user as a code-written notice plus a stored file. Smart reads it only on request, wrapped as
  untrusted.
- No path lets worker output steer a model turn, write memory or start new tasks without the user's confirmation.
- The user controls the workers from the Cabinet: workers, keys, OAuth grants, wake-up targets and destination.

**Non-goals.**
- No knowledge in code of what any task is about (D1).
- No push of task content to a worker.
- No file or binary results in the first version (§5.9).
- No worker access to memory, drive, mail or other tasks through this surface. A task's own files (§5.12) are copies
  the user confirmed with the task, not access to the places they came from.
- No scheduling of tasks by the bot itself: a task exists only because the user asked in a chat turn.
- No voice hand-off: Lelik neither creates nor reads tasks (D11).
- No task status in background runs (E2).
- No user-facing switch for the feature (D12).
- No guarantee that any worker comes. The bot reports what happened (§7).

## 5. Design

### 5.1 Entities and their life

Collections are named in `EnvironmentConfig`, with the environment prefix, and every client passes `database=`.

**Worker** (`{prefix}external_workers`, doc id `{user_id}:{worker_id}`). Fields:
- `worker_id`: random and immutable.
- `user_id`, `account_id`.
- `label`: unique per user, short, from a restricted character set. This is the name Smart and skills use.
- `description`: optional one line written by the user and shown to Smart.
- `destination`: `workspace` (the default) or `drive` (§5.9).
- `wake`, plaintext part: `wake_host`, shown in the Cabinet, and `has_wake`.
- `wake`, secret part: one `CipherPort` ciphertext of `{url, headers}`, AAD `external_workers/{user_id}:{worker_id}`
  (E3). The URL is encrypted too, because users put tokens in query strings.
- `wake_generation`: an integer.
- `status`: `active` or `revoked`.
- `created_at`, `updated_at`.

**Worker key** (`{prefix}external_worker_keys`, doc id = `keyed_digest(SECRET_DIGEST_KEY, "external_worker_key",
key)`). Fields: `user_id`, `account_id`, `worker_id`, `hint` (a short tail, as invite codes keep one), `created_at`,
`last_used_at`, `revoked_at`. The raw key is stored nowhere. A key has no expiry (E6).

**Task** (`{prefix}external_tasks`, doc id `task_id`, random). Fields:
- `user_id`, `account_id`, `worker_id`, `label` (copied at creation).
- `title` and `text`: the title is short; the text is the full brief. Both are written by Smart and both are capped.
- `origin`: the session and channel of the creating turn. Notices go there.
- `status`: `held`, `queued`, `leased`, `done`, `failed`, `cancelled` or `expired`.
- `hold`: for a held task only, `code_digest` (the confirmation code's keyed digest) and `hold_until`.
- `lease`: `lease_id`, `until`, `generation`, `renewals`.
- `claims`: a count of every claim, whether the lease was released or expired.
- `result_meta`: `bytes`, `sha256`, `submitted_at`, and `submitted_via` (`key` or `oauth`, plus the client id).
- `failure`: a code, plus the worker's reason, capped and stored as untrusted text.
- `delivery`: `state`, `destination`, `path`, `delivered_at`.
- `created_at`, `updated_at`, `deadline_at`, `purge_after`. `purge_after` is set when the task reaches a final state.

**Result** (`{prefix}external_task_results`, doc id `task_id`). Fields: `text`, `purge_after`.
- This is the record of truth for the result body. It is kept apart from the task, so listings never read bodies.
- The document is **create-only**: it is written with a create that fails if the document exists, inside the submit
  transaction. A result is never overwritten.
- `RESULT_MAX_BYTES` stays below the 1 MiB Firestore document limit.
- The destination file (§5.9) is a copy for the user, so a full workspace or an absent drive never loses a result.

The two `purge_after` collections join the TTL runbook (`docs/07_deployment/README.md`) and `test_ttl_registry.py`.
Composite indexes, which must be `READY` before the deploy that queries them:
- claim: `user_id`, `worker_id`, `status`, `created_at` ascending;
- lease recovery: `user_id`, `worker_id`, `status`, `lease.until`;
- listing: `user_id`, `created_at` descending.

**Life of a task.**

```
create, untainted ──────────────────────► queued ──claim──► leased ──submit──► done ──► delivered
create, tainted ──► held ──$task send──►  ▲  ▲               │ │ │
                     │                    │  └──release──────┘ │ ├─renew (lease extended, ≤ MAX_RENEWALS_PER_TASK)
                     └──hold_until──► expired└──lease expired──┘ └─fail───► failed
held|queued|leased ──user cancels──► cancelled
queued|leased ──deadline_at passes (timer, or found by a claim)──► expired
a claim that would be the task's MAX_CLAIMS_PER_TASK + 1st ──► failed (code: claim_loop)
```

`done`, `failed`, `cancelled` and `expired` are final. A final task never changes again, except for its `delivery`
fields.

### 5.2 The service

`ExternalTaskService` (`src/services/`) is the one place where the rules live. It sees storage through
`ExternalTaskStore` and `ExternalWorkerStore` (ports in `src/ports/`, Firestore adapters in `src/adapters/`) and enqueues
through `TaskQueue`.

**Every state change is one Firestore transaction with a precondition.** For a worker's operation the precondition is
`status = leased ∧ lease.lease_id = <presented> ∧ lease.generation = <stored at claim>`. If the precondition no longer
holds, the operation is refused with a reason code and writes nothing. That is how races resolve:
- Of two concurrent claims, one wins and the other takes the next task.
- Cancel against submit: cancel's precondition is `status ∈ {held, queued, leased}`, and submit's is the lease
  precondition. Whichever commits first wins. A submit after cancel is refused and stores nothing. A cancel after done
  is refused ("already finished").
- A lease timer against a renewal or a submit: the timer's precondition includes `lease.until ≤ now`, so a renewed or
  finished lease is left alone.

Operations:

- `create(user, worker_label, title, text, origin, tainted)`
  - Checks, all of them fresh: the flag (§5.10); the worker exists, is active and has that label; the caps on text size
    and open tasks per user.
  - Untainted: writes the task as `queued`, enqueues the deadline timer (§5.8) and starts a wake-up when §5.8 calls for
    one.
  - Tainted (§5.3): writes the task as `held` with a fresh confirmation code (stored as its keyed digest) and
    `hold_until = now + HOLD_S`. No timer except the hold's, and no wake-up.
  - Returns the task id, and for a held task the code.
- `confirm(user, code)`: reached only from the owner's `$task send <code>` command (§5.3), or its Slack «Send» button
  (decision record `external_task_confirmation_and_alerts.md`, item 2). A `held` task of that user
  whose code matches and whose hold has not run out becomes `queued`. Then the deadline timer and the wake-up run as
  for an untainted create. A wrong or expired code changes nothing and gets one neutral line.
- `list(user, filters)` returns the user's own tasks: id, worker, title, status and times. It never returns a result or
  a failure reason.
- `get(user, task_id)` returns one task. A result and a failure reason both come back wrapped (§5.3).
- `cancel(user, task_id)`: a non-final task becomes `cancelled`.
- `claim(principal)`
  - `principal` = (user, account, worker) from the verified credential (§5.6, §5.7). It is never taken from arguments.
  - Checks the flag, the account lock and the rate limit.
  - Takes the oldest `queued` task of that worker. If there is none, it takes the oldest `leased` task whose
    `lease.until` has passed.
  - **A candidate past its `deadline_at` is expired, not claimed.** The claim sets it `expired` in its own transaction,
    enqueues the expiry notice and moves on to the next candidate. A lock can swallow the deadline timer (`/worker`
    refuses a locked user), and this keeps a stale task from being handed out once the user is unlocked.
  - A candidate whose `claims` already equals `MAX_CLAIMS_PER_TASK` fails with code `claim_loop` (a notice follows)
    instead of being claimed.
  - The claim transaction's precondition is that status and lease generation are unchanged.
  - Sets `leased`, a new `lease_id`, `until = now + LEASE_S`, `generation + 1`, `renewals = 0` and `claims + 1`.
  - Enqueues the lease timer.
  - Returns the task's id, title, text, `created_at`, `lease_id` and `until`.
- `renew(principal, task_id, lease_id)` (E1)
  - The lease precondition, plus `lease.until > now` (an expired lease cannot be revived) and
    `renewals < MAX_RENEWALS_PER_TASK`.
  - Sets `until = now + LEASE_S`, adds one to `renewals` and enqueues a lease timer for the new `until`.
  - At the cap the renewal is refused (`renew_cap`). The lease then runs out normally.
- `submit(principal, task_id, lease_id, text)`
  - Normalises and caps the text (§5.9).
  - Accepted in either of two cases:
    - The lease precondition holds. An expired lease that nobody re-claimed still holds it, because the status is
      still `leased` and the generation is unchanged.
    - The task is already `done` with the same `sha256` from this worker: an idempotent replay that gets the same
      success answer and changes nothing.
  - Anything else is refused with a reason code. A different body after `done` is refused, and the create-only result
    document makes a replacement impossible.
  - On acceptance, one transaction sets `done` and creates the result document. Delivery is then enqueued (§5.9). It
    runs in a Cloud Task, never inside the worker's request, because Cloud Run throttles CPU after the response.
- `release(principal, task_id, lease_id)`: under the lease precondition, the task goes back to `queued` and its
  generation moves on. The release counts toward `MAX_CLAIMS_PER_TASK` through the claim that preceded it.
- `fail(principal, task_id, lease_id, reason)`: under the lease precondition, the task becomes `failed`. The reason is
  stored capped and untrusted, and the user gets the code-written failure notice (§5.9).

The service imports no adapter and no other service (REQ-ARCH-22). Delivery collaborators (notification, workspace,
drive) are injected by composition.

### 5.3 Smart tools

*Built as the task specialist's tools (D14): Smart offers the intent `work_with_external_tasks`, the specialist holds
the tools below.*

There are four local tools, served by the `DelegationEngine` like `workspace`. Each is a declaration builder plus a
per-execution handler factory in `src/infrastructure/external_task_tools.py`, wired in `SmartResponseAgent` next to the
existing ones. They are CRUD-shaped: one operation per tool, a read operation exists, and no omitted field destroys
data.

| Tool | Parameters | Does |
|---|---|---|
| `create_external_task` | `worker` (enum of the user's labels, built per execution), `title`, `text` | `create`; the result says `Tool note:` with the task id, and whether the task is queued or held for the owner's confirmation |
| `list_external_tasks` | optional `status`, optional `worker` | the user's tasks: id, worker, title, status, times; never a result, never a failure reason |
| `get_external_task` | `task_id` | one task's status; when done its result, and when failed the worker's reason, each wrapped by `wrap_untrusted(store=workspace)` (`external_content`) after `EXTERNAL_REMINDER` |
| `cancel_external_task` | `task_id` | `cancel` |

**Where the tools exist.**
- All four exist only on interactive turns, gated on `interactive_delivery` as `draft_skill` and
  `propose_memory_facts` are, and only for a user with the flag.
- A background run (the daily review, a voice errand, `/worker`) therefore cannot enqueue and cannot read status (E2);
  a fired reminder's run is the one exception (`task_entry`, `EXTERNAL_TASK_CONFIRMATION_AND_ALERTS`: its create is queued). No task can spawn a task without the owner in the chat.
- That gate alone does not answer the question of who asked. `interactive_delivery` means the user is present, not that
  the user asked for this call (§3). The taint rule below closes the gap.
- None of the four tools sets `history_context`. Nothing they return is copied into the persisted turn's metadata
  (`smart_response_agent.py:660-661`), so a result read in one turn is not carried into later turns by the tool layer.

**Taint and the held task. (Superseded 2026-11-24: every chat create is held; see the decision record.)** An injected instruction in outside content that Smart read during the turn could make
Smart call `create` in that same turn. The wake-up would then leave at once.

The defence is per-execution taint:
- The handlers are built per execution, so one `TaintTracker` object is built with them and shared between the
  `DelegationEngine` and the `create` handler.
- **The execution is tainted** as soon as any of these enters it:
  - a tool result that holds an untrusted wrapper (`external_content` or `drive_document`, matched by the look-alike
    pattern of `untrusted_text.py`) or an attached-file reminder;
  - a file part;
  - any tool result whose source is not on a closed allowlist. The allowlist is `use_skill`, the memory search intent,
    `list_external_tasks`, `cancel_external_task`, `create_external_task`, and `workspace` reads of files that carry no
    untrusted flag (§5.9).
- The same check runs once on the request before the first model call: history, current message parts and static
  blocks. A chat upload, or a wrapper already in history, taints the execution from the start.
- The allowlist matters because web search output is not wrapped at all (§3). A rule that looked only for wrappers
  would miss a web page.
- The check fails safe. A false positive (the user pasted a tag) only makes a task wait for a confirmation.
- `get_external_task` returning a result or a reason taints the execution itself.

**What a tainted create does. (Superseded 2026-11-24: this is now what every chat create does.)**
- It does not queue. The service stores the task as `held`, and no wake-up leaves.
- The handler returns a delivery item that the turn's handler posts literally after the answer. It quotes the worker,
  the title, the **full task text** and the command `$task send <code>`.
- Only the owner's `$task send <code>` (or, on Slack, the owner's press of the «Send» button under it, which ends in the
  same `confirm`) queues the task and sends the wake-up. It follows the `$skill save` /
  `$memory save` pattern (§3), and `$` commands never reach a model or history.
- An unconfirmed hold runs out at `hold_until` and becomes `expired` silently.

**Every create is visible.**
- An untainted create also posts a literal receipt as a delivery item, the way drive-note receipts are posted
  (`conversation_handler.py:323`): "Handed to *research*: *<title>*", then the full task text, then the task id.
- `TASK_TEXT_MAX_CHARS` is bounded so the whole text fits one literal post. A receipt never cuts the text, so the owner
  never confirms text they have not seen.
- The owner therefore sees every task that leaves the system, word for word, whatever the model said in its answer.

The user id is closed over, never an argument. A label that does not exist, a task of another user and a missing task
all get the same `Tool error:` line, so nothing reveals whether a task exists.

**Texts.**
- The four tool descriptions are model-facing prompt text. They state what the queue is for, that a skill decides when
  to use it, that results are outside content, that every task is shown to the owner, and that a task shaped by outside
  content waits for the owner. Then they state the rules.
- They ship by code: `src/domain/external_task_texts.py` (root `CLAUDE.md`, "Two delivery paths"), as the other Smart
  local tools' texts do.
- The receipts, the held notice and the delivery notices are UI strings in `src/locales/`. `$task send` is one more
  `$` command in `ConversationHandler.handle_command`.
- This RFC does not write the final wording.

### 5.4 Worker MCP resource

There is a third protected resource, `MCPResource.TASKS`, served at **`/tasks-mcp`**. It is not under `/mcp/` (§3), and
its protected-resource metadata path is checked before the facts prefix match. Its scope is **`external_tasks`**. The
scope string lives in composition and is added to `SCOPE_OF`.

**Tools**, liberal input as in `decisions/mcp_tool_schema_liberal_input.md`:

| Tool | Parameters | Does |
|---|---|---|
| `list_tasks` | none | the tasks of this worker that are `queued` or leased to it: id, title, created, lease |
| `claim_task` | none | `claim`; "no task" is a success text, never an error |
| `renew_lease` | `task_id`, `lease_id` | `renew` |
| `submit_task_result` | `task_id`, `lease_id`, `result` | `submit` |
| `release_task` | `task_id`, `lease_id` | `release` |
| `fail_task` | `task_id`, `lease_id`, `reason` | `fail` |

No tool takes a user, an account, a worker or a label. All of them come from the verified credential. A `held` task is
invisible to every worker tool.

Each call runs a `_Caller`-shaped wrapper, as the drive app does:
1. Takes the principal from the token.
2. Refuses a locked account (`ACCOUNT_LOCKED_TOOL_ERROR`).
3. For an OAuth token, calls `grant_allowed(user, gid)` (§3). That is the same check the `/d/` link makes; it includes
   the whitelist. A revoked grant is therefore refused on its next call, not when its access token expires.
4. Refuses a user without the flag, or a revoked worker, with one neutral line.
5. Applies the per-key or per-grant rate limit.
6. Runs the service operation and writes one span and one `[TaskMCP]` log line. The line never carries task text, a
   result, a key or an `Authorization` header.

**Texts** come from two Firestore prompt tokens, `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`, loaded and
validated like `MCP_DRIVE_TOOL_TEXTS`. They explain first: the worker is doing work for the user; the task text was
written by the user's assistant and may quote outside material; the result will be shown to the user as outside
content. Then the rules.

**The claim result separates provenance from content.** Code-written fields (task id, created time, "written by the
user's assistant at the user's request") come first. The task text follows as its own field. The worker side is
responsible for treating it as data. This RFC cannot enforce that (§6.1).

**OAuth** (D8, for claude.ai custom connectors):
- **Not a default scope.** `external_tasks` joins `valid_scopes` but **not** `default_scopes`. A registration that
  names no scope gets facts and drive, as today, so an existing connector never gains queue access.
  - A client that wants the queue must register with `scope` containing `external_tasks`, then authorize with
    `resource=<tasks URI>` or with `scope=external_tasks` alone.
  - `bind_resource` gains the third resource. A scope set that is exactly `{external_tasks}` selects it, and a mix is
    refused, as today.
- **The grant is bound to one worker.** For the tasks resource, the consent page also lists the user's active workers
  and requires the user to pick one. The chosen `worker_id` is stamped on the code and the refresh token, and copied
  into the access token as a `wid` claim.
  - A tool call whose `wid` names a revoked or foreign worker is refused.
  - So an OAuth client sees exactly one worker's tasks (D9). It can never choose a label per call.
- **The flag is checked at grant time and on every call.** `issue_auth_code_for_consent`, the code exchange and the
  refresh rotation refuse the tasks scope for a user without the flag. `_allow_reason` gains a resource-aware variant
  for this. Every tool call reads the flag again.
- **Grants are visible and revocable.** The Cabinet lists each worker's active OAuth grants: client name, granted
  time, last refresh. Revoke revokes every refresh token of that `grant_id`, and step 3 of the call wrapper then refuses
  the grant's live access tokens.

**Worker keys on MCP** (D8): the resource's verifier routes on the token's shape, as the drive verifier routes on the
voice `kid`.
- A credential with the worker-key prefix goes to the key verifier (§5.6) only.
- Anything else goes to the OAuth verifier only. That verifier also requires `aud` = the tasks URI.
- A worker key presented to `/mcp` or `/drive-mcp` is refused there: those verifiers know nothing of keys.

**Dispatcher and the entry-point test.**
- `mcp_owner` gains the tasks paths, before the facts set. `build_mcp_parent_asgi` gains a third app and enters its
  session manager.
- `MCP_PATHS` gains `/tasks-mcp`, plus an `ENTRY_POINTS` row `mcp_task_tools` with its probe.
- `test_the_indexer_and_mcp_paths_are_classified` asserts `_mcp_literals() | {DRIVE_PATH} == set(MCP_PATHS)`, so it
  needs the new path constant too. That is a test edit, and it goes through the reviewer rule.

### 5.5 Worker REST endpoint

The routes are under **`/external-tasks/v1/`**. They are not under `/api/`, because every `/api/` route counts as
behind the Cabinet cookie gate (§3) and these routes are not. Auth is a worker key in `Authorization: Bearer` only;
OAuth tokens are not accepted here. Bodies are JSON, and answers are JSON with a stable `code` plus a text from
`MCP_TASK_RESULT_TEXTS`.

| Method and path | Operation |
|---|---|
| `GET /external-tasks/v1/tasks` | `list_tasks` |
| `POST /external-tasks/v1/claim` | `claim_task`; `204` when there is no task |
| `POST /external-tasks/v1/tasks/<task_id>/renew` | `renew_lease` (`{lease_id}`) |
| `POST /external-tasks/v1/tasks/<task_id>/result` | `submit_task_result` (`{lease_id, result}`) |
| `POST /external-tasks/v1/tasks/<task_id>/release` | `release_task` |
| `POST /external-tasks/v1/tasks/<task_id>/fail` | `fail_task` (`{lease_id, reason}`) |

Status codes:

| Status | When |
|---|---|
| `401` | missing, malformed or unknown key; one body for all three |
| `403` | locked account (the lock text), flag off, or revoked worker |
| `404` | a task that is not this worker's, or a missing task; one body for both |
| `409` | stale lease, renewal cap, or already submitted with another body |
| `413` | over a size cap |
| `415` | wrong content type |
| `429` | rate limited, including the 401 limit (§5.6); carries `Retry-After` |

The handlers sit in one Quart blueprint (`src/web/`), each a thin call to the service. Each route is a `ROUTE_ROWS`
entry mapped to a new `ENTRY_POINTS` row, `external_task_api`, with its probe.

### 5.6 Worker registry and keys (Cabinet)

**Placement.** A new row, **External workers**, goes in Settings → Connections (`CABINET_REDESIGN_RFC.md` §5.1). Its
collapsed status reads "n workers". Only users with the flag see the row. Each new Cabinet operation is added to the
operations inventory (§6a) in the same PR, by review.

**Body**, low fidelity:
- A list of workers. Each shows its label, description, destination, wake-up host or "polls", key hints with created
  and last-used times, OAuth grants with client name and times, and the last task time.
- **Add worker**: label, optional description, destination (workspace; drive only when §5.9's drive option is built
  and `can_save_to_primary` holds).
- **Edit worker**: description, destination, wake-up URL and headers. Header values are write-only: the page shows
  header names, never values. Changing the URL or a header replaces the whole ciphertext.
- **Mint key**: the key is shown once, with a copy button, and never again. A worker can hold more than one key, for
  rotation.
- **Revoke key**, **Revoke grant** (§5.4) and **Remove worker**. Removing a worker revokes its keys and grants and
  cancels its non-final tasks.
- There is no switch for the feature itself (D12).

The Cabinet calls `/api/external-workers…` routes, which sit behind the Cabinet gate.

**Key format and check.**
- A key is a fixed prefix followed by 256 random bits, URL-safe. The prefix makes it routable (§5.4) and recognisable
  to secret scanners.
- It is stored as its keyed digest (SECRETS_AT_REST's verify-only class), the way MCP codes are.
- **Shape first.** Prefix, exact length and character set are checked before any lookup. A malformed key is a `401`
  with no store read.
- **Unknown keys are bounded.** `401` answers are counted per client address (the first hop Cloud Run's front end
  reports) and globally, each in a fixed window. Above either limit the endpoint answers `429` before any lookup, so a
  flood of guesses causes no reads.
- A well-formed key is then checked in this order: digest lookup (one read), not revoked, worker active, user exists,
  not locked, whitelisted (the same reasons as `_allow_reason`), flag on. Then `last_used_at` is updated, coarsened so a
  busy worker does not write on every call.
- Keys do not expire (E6). Revocation is immediate, and the Cabinet shows last use.
- No log line carries the key. Lines name the worker by `fingerprint(worker_id)` and the user by the first 8
  characters of the id.

**Compared with the voice token** (D8):
- Reused: routing on the credential's shape before verification, so each verifier sees only its own kind; the tenant
  from a server-side record, never from the presented value alone; the whitelist and lock re-read on use; the logging
  rules.
- Not reused: the JWT and its signing key. A key must be revocable at once and checked against a server record on
  every call anyway, so a signature adds nothing that the digest lookup does not give.
- Not reused: the ephemeral store. It is TTL-bound, and a worker key is durable until revoked.
- Like codes and drive links, every key dies when `SECRET_DIGEST_KEY` changes. This is listed in §7.

**Wake-up secrets** (E3). D3's wording was "through the existing credential store". That store is OAuth-shaped: its
payload is a token pair, and its `save` refuses a record without a provider account subject (§3). Wake-up headers have
no provider account. The worker record therefore holds them as one `CipherPort` ciphertext with AAD bound to the
record: the credential store's primitive and rule, used the way `mcp_clients` uses them for client secrets.

### 5.7 Principal and tenancy

Every worker-side call resolves one principal, `(user_id, account_id, worker_id)`:
- from a worker key: the key record;
- from an OAuth token: `sub`, `account_id` and `wid`, with the worker record re-read and the grant checked.

Every store query is keyed by that principal. No surface accepts user, account, worker or label from input. A task of
another worker of the same user is as invisible as a task of another user.

### 5.8 Wake-up and timers

**When a ping is due.** When a task becomes `queued` (an untainted create, or a confirmation) and it is the first
`queued` task of a worker that has a wake-up target and no live lease, the service:
1. increments `wake_generation` in the same transaction as the task write;
2. enqueues `/worker` task type **`external_task_wake`** with `{user_id, worker_id, generation}` and dedup key
   `external_task_wake:<worker_id>:<generation>`.

Rhythm comes from the stored generation, not from the task name (`decisions/worker_task_dedup.md`).
- A second task created while the first is still queued adds no ping, because the generation is unchanged.
- No ping is sent while any task of the worker holds an unexpired lease: the worker is draining.
- A `held` task never causes a ping.
- After a worker submits, fails or releases a task while others of its tasks are queued, the service pings for the
  worker's oldest queued task (same lease check, so no ping while another task is leased). A replayed submit does not.

**The wake task.** `ExternalTaskWakeTask` (a `WorkerTask`, registered in `build_worker_tasks`, plus a row in the
SCHEDULERS.md task-type map):
1. Re-reads the worker. A changed generation, a removed target or no queued work means it answers 200 and sends
   nothing.
2. Decrypts the target and calls `OutboundWebhookPort.post(url, headers, body)`.
3. The body is `{"event": "tasks_available", "worker": <opaque worker reference>}`. It never carries task text, title,
   label or count.
4. A network error or a 5xx from the target answers 5xx, so Cloud Tasks retries with the queue's backoff.
5. A guard refusal or a 4xx is final. It answers 200 and posts one code-written notice to the user: "the wake-up of
   *<label>* was refused: <code>". The code comes from the closed `WakeRefusal` list below, never from an HTTP reason
   phrase or a response body.
6. The task is retried a bounded number of times (the queue's own setting, set at implementation). Retries never
   duplicate tasks; at worst the worker is woken twice and finds one task.

**Timers**, `/worker` task type **`external_task_timer`**, with no Scheduler job:
- **Lease timer.** Enqueued at each claim and each renewal, delayed to that `lease.until` plus a margin. Its dedup key
  is `lease:<task_id>:<generation>:<renewals>`.
  - If the task is still leased under that generation and `lease.until` has passed, it returns the task to `queued`.
  - Otherwise (renewed, submitted, released, failed) it does nothing.
  - When it re-queues work, it pings again under a new generation if the worker has a target (D4).
- **Deadline timer.** Enqueued when a task becomes `queued`, delayed to `deadline_at`. If the task is not final, it
  becomes `expired` and the user gets a notice. `TASK_MAX_AGE_S` must fit within Cloud Tasks' maximum schedule delay;
  if it does not, the timer re-enqueues itself.
  - A locked user's timer is refused by `/worker`. So the claim also expires any candidate past its deadline (§5.2).
- **Hold timer.** Enqueued for a held task at `hold_until`. It expires the hold silently.
- The claim also recovers an expired lease lazily (§5.2), so a lost timer delays recovery but never loses a task.

**Outbound-request guard** (D3). The guard is a new adapter, `src/adapters/outbound_webhook/`, behind
`OutboundWebhookPort`. It is the only code that sends to a user-entered URL.

- **Scheme, port, name.**
  - `https` only, port 443 only (E4), no userinfo.
  - The host must be a DNS name with at least two labels. An IP literal, a single-label name and any name under
    `.internal` (which covers the platform's metadata server name) are refused **before** resolving.
  - **The bot's own hosts are refused.** The list is built at boot from every deploy unit's configured URL: the main
    service, the indexer, the voice relay, both sandbox services, the MCP resource host and the custom domains, all read
    from their deploy keys and never written in code. Any name under `run.app` is refused too.
  - A consequence: a worker hosted on Cloud Run's default domain must be reached through a custom domain.
- **Addresses.**
  - Resolution is asynchronous and runs inside `WAKE_TIMEOUT_S`.
  - Every resolved address must be globally routable. Refused: private, loopback, link-local (which covers the metadata
    address), CGNAT, multicast, reserved, unspecified and documentation ranges.
  - IPv4-mapped IPv6 addresses are checked as IPv4. Addresses that embed IPv4 are refused outright: NAT64
    `64:ff9b::/96`, 6to4 `2002::/16`, Teredo `2001::/32`.
  - The connection goes to the vetted address, with SNI and `Host` set to the name, so a DNS answer cannot change
    between the check and the connect (rebinding).
- **Transport.** `follow_redirects=False`: a 3xx is a failure. `trust_env=False`: no proxy from the environment. Short
  connect and total timeouts (`WAKE_TIMEOUT_S`).
- **Headers.**
  - User headers must be valid token names.
  - Hop-by-hop and framing headers (`Host`, `Content-Length`, `Transfer-Encoding`, `Connection`) are refused, at save
    time and again at send time.
  - Header count and size are capped.
- **Response.** The answer body is read up to a small cap and discarded. It is never stored, never logged, never shown
  to a model.
- **Logs.** A log line carries the host and an error class only. It never carries the URL, a header, a status line's
  reason phrase or a body.
- **Refusal codes** form one closed enum, `WakeRefusal`: scheme, port, name, own host, internal name, address, DNS
  failure, redirect, client error, header. Only these reach the user's notice.
- **When the guard runs.** Once when the Cabinet saves the target (the user gets an immediate error) and again on every
  send, because DNS changes.
- **Egress assumption.** The guard relies on the main service having no VPC egress (§3): the only network it reaches
  is the public internet. If the main service ever gets a VPC connector or network, the guard must be revisited. Its
  address rules do not cover a private network's own ranges or names. That revisit is a deploy-time trap for the
  building block README.

### 5.9 Result delivery

*Superseded in part (2026-11-24): the taint of reads below is dropped, and a literal notice is replaced by a system alert to the bot (decision record `external_task_confirmation_and_alerts.md`).*

**Result format, v1** (D9):
- Text only: `text/plain` or `text/markdown`, UTF-8. An invalid sequence is refused, not repaired.
- Capped at `RESULT_MAX_BYTES`.
- Unicode-normalised (NFC). Control characters stripped, except newline and tab. Bidi overrides and other invisible
  format characters stripped.
- The system tag word is neutralised (`neutralize_system_tag`) before storage, so a stored result can never forge a
  system note.
- A failure reason gets the same treatment, under `FAIL_REASON_MAX_CHARS`.

**No files in v1.** A file result means parsing hostile bytes. The only sanctioned place for that is the sandbox,
through the document service (`docs/05_building_blocks/file_conversion/README.md`,
`docs/05_building_blocks/untrusted_content_sandbox/README.md`). It never happens on the main service. Text covers the
intended results.

When files are added, a result file becomes an external file ref opened through that one path, and nothing on the
main service parses or renders it. This is a later RFC step, not v1.

**Links** in a result stay text. Nothing follows them: no unfurl, no prefetch, no preview. The posted notice never
quotes the result.

**Delivery task.** `/worker` task type **`external_task_deliver`**, dedup key `deliver:<task_id>`, enqueued by
`submit`. It is idempotent through `delivery.state`.

1. **Write the copy** to the worker's destination. The bot chooses the name and folder; the worker's title or
   filename is never used. The copy starts with a code-written outside-content header (task id, worker label,
   submitted time, "outside content from an external worker"), then the result.
   - **workspace** (default, always available): `references/external_tasks/<yyyy-mm-dd>-<task_id short>.md`, written
     through `WorkspaceService` with origin `background:external_task:<task_id>`.
     - The write sets a file-level **untrusted flag** in the version's object metadata
       (`untrusted=external_task:<task_id>`).
     - Every later version of that file keeps the flag, including `str_replace`, `insert` and `restore`. So do
       `rename` and `copy`: the copy and the renamed file carry it.
     - **Every workspace read of a flagged file is wrapped** with `wrap_untrusted(store=workspace)`: `view`, `grep`
       output lines, and the `workspace:` ref path. The marking follows provenance, not the folder.
   - **drive** (optional, later step; D13): `UserDriveService.save_to_primary(..., folder=(DEFAULT_INBOX_FOLDER,
     <localized external results folder>))`.
     - It lands in a bot-named subfolder of the primary drive's Inbox, the way call text files land in
       `Inbox/<voice calls folder>` (§3). It never overwrites. It is offered only while `can_save_to_primary` holds.
     - **That subfolder maps to the `external_content` tag, never to `drive_document`.** One predicate on the canonical
       path (`is_external_results_path`) is used by every drive render: search cards, passages and opens.
     - The drive specialist therefore sees these files wrapped as outside content, in chat and in background runs
       alike.
     - The drive connector sees them only if the user's connector access includes that folder, and then wrapped the
       same way.
     - The indexer processes them as it processes any drive file, which is already untrusted input to it.
     - A file the user moves out of the subfolder loses the mapping. This is a residual risk, listed in §6.1; the
       outside-content header stays in the text.
   - A refused copy (workspace caps, no primary drive) is no loss. The result stays in the result record. The notice
     says the copy failed, and `get_external_task` still reads the result.
2. **Post the notice**, with no agent turn. This is the `notify_drive_note_receipt` shape (§3):
   - A localized UI string is posted with `send_literal` to the task's origin channel, through `resolve_channel`,
     which also refuses a locked user. Example: "*research* finished task *<title>* (task *<id>*). The result is in
     your workspace: `references/external_tasks/2026-11-23-a1b2c3.md`."
   - Then one code-written history pair is appended to that channel's session:
     - user line: `[System: external task <id> for worker <label> finished; result stored at workspace:<path>]`;
     - model line: `[external task result notice posted: workspace:<path>]`.
   - Neither line carries the result, the failure reason or the title (E5).
3. Set `delivery.state = delivered`.

Failure, expiry and cancellation notices use the same shape with their own UI strings. A failure notice says that the
worker reported a failure and how to read its reason. It never quotes the reason.

**Why this shape.**
- No model reads the result or the notice while it is delivered, so no result text can choose a tool. The result is
  never pulled into a background agent turn (D9).
- The history pair is code-written. It carries an id, a label and a path, so the consolidator, which reads its user
  line as the owner's words, finds nothing to extract. This is the same reason the drive-note event line is safe (§3).
- In later turns, the fingerprint rule reads a persisted `[System: …]` pair as a record, not an order
  (SYSTEM_TURN_FINGERPRINT_RFC §4.4). That is what lets "summarise it" resolve to this task.
- `wrap_system` plays no part. It keys only in-memory text of the current turn, and notify turns are unkeyed by that
  RFC's §8. Safety comes from the line's content, not from a key.

**Who can read a result, and when.**
- Through `get_external_task`: only in an interactive turn, wrapped.
- Through `workspace` or the drive tools: **in any turn, background runs included.** A reminder can name the file
  (`reminder_alert.py:33`), and `workspace` exists on every path (§3). Such a read is always wrapped, by the untrusted
  flag or by the drive path mapping, and it taints the execution (§5.3).
- A background run that reads a result still cannot enqueue: it has no queue tool at all.
- The earlier claim that no background turn ever sees a result does not hold. What holds is that every read is wrapped
  and no read can lead to an unconfirmed task.

**Alternatives to this shape** (D6 asked for the comparison):
- **`notify(...)` with a code-written alert.** It runs an agent turn on the alert. For a Smart recipient that turn
  holds the delegation tool and `workspace`. The alert must carry the model-written title, which may echo outside
  content (§6.1), and the persisted user line is the raw alert. This is rejected under D9: it puts model-written text
  into a tool-holding background turn and into a user-role line, for no gain over a literal notice.
- **`notify_raw(..., literal=True)` alone.** It is equally safe, but it writes no history, so a follow-up such as "open
  it" has nothing to resolve against. The pair above fixes that.

### 5.10 Enablement flag

D7's flag is `UserBotConfig.external_tasks_enabled`, false by default.
- It is set only by an operator, by a direct database write: a script or the console (D12).
- No Cabinet route, no `$` command and no API writes it.

It is read fresh by:
- the service, on every operation;
- the MCP grant path, at consent, code exchange and refresh;
- every MCP tool call;
- every REST call;
- the Cabinet routes, which hide the External workers row without it.

Smart's tool gate reads the agent's profile, which the factory may cache for up to an hour. During that window a user
switched off may still see the tools, but the service refuses every call.

Switching the flag off does not delete anything. Existing tasks run to their deadlines without being served.

### 5.11 Limits

These are named constants in `src/domain/external_task.py`. The numbers are set at implementation (E7).

| Constant | Bounds |
|---|---|
| `TITLE_MAX_CHARS`, `TASK_TEXT_MAX_CHARS` | what Smart may write into a task; the text fits one literal receipt |
| `RESULT_MAX_BYTES` | a submitted result; below the 1 MiB Firestore document limit |
| `FAIL_REASON_MAX_CHARS` | a worker's failure reason |
| `MAX_OPEN_TASKS_PER_USER` | non-final tasks per user, held ones included; `create` refuses above it |
| `MAX_WORKERS_PER_USER`, `MAX_KEYS_PER_WORKER` | the registry |
| `LABEL_MAX_CHARS`, `LABEL_PATTERN` | labels (also the tool enum) |
| `LEASE_S` | one lease, and one renewal's extension |
| `MAX_RENEWALS_PER_TASK` | renewals within one lease |
| `MAX_CLAIMS_PER_TASK` | claims of one task, releases and expiries included, before `claim_loop` |
| `HOLD_S` | how long a held task waits for `$task send` |
| `TASK_MAX_AGE_S` | `deadline_at` |
| `RETENTION_S` | `purge_after` after a final state |
| `MAX_FILES_PER_TASK`, `TASK_FILES_MAX_BYTES` | files on one task; each file also within the send cap of `resolve_file` |
| `FILE_LINK_TTL_S` | one file link (§5.12) |
| `CLAIMS_PER_WINDOW`, `SUBMITS_PER_WINDOW`, `CALLS_PER_WINDOW`, `RATE_WINDOW_S` | per key, and per OAuth grant on MCP |
| `UNAUTHORIZED_PER_ADDRESS_PER_WINDOW`, `UNAUTHORIZED_GLOBAL_PER_WINDOW` | `401` answers before the `429` |
| `WAKE_TIMEOUT_S`, `WAKE_HEADERS_MAX`, `WAKE_HEADER_BYTES_MAX` | wake-up, DNS included |

The rate counters are fixed-window counts in a store that every instance sees. The store is chosen at implementation.
An in-process counter is not enough, because Cloud Run runs several instances.

### 5.12 Task files (D15)

**Why.** A brief is text, and the work often needs the material the user already has: a document, a spreadsheet, a page
of earlier results. Copying it into the brief fails on size and on any binary format, and loses the original. Giving
the worker access to where the material lives (the drive, the workspace) breaks the queue's boundary: the worker would
see everything there, not what it was given, and a shared folder needs someone to clean it. So a task carries copies of
exactly the files it was given, and the worker reaches them only through the task.

**Attaching.** `create_external_task` gains an optional `files` argument: a list of file references, each one of the
kinds `classify_ref` knows (workspace, drive, chat, email attachment, delivered, skill). Each reference must appear in
the specialist's instruction, as the brief must (`ERROR_NOT_THE_BRIEF`), so the specialist attaches what Smart named and
nothing else. Before the task is stored, each reference is resolved by `FileConversionService.resolve_file(ref,
user_id, action="send")`: the original bytes, with the caps and refusals of sending a file to the user (an email
attachment's active-kind check included). Nothing is parsed or converted. A reference that does not resolve refuses
the whole create with one tool error naming it: a task never leaves with part of its files.

**Storing.** Each file is written once to the private media bucket at `external_tasks/{user_id}/{task_id}/{n}-{name}`
(`MediaStoragePort.store`; the name sanitised). The task record keeps `files`: for each, its number, name, MIME type,
size and storage key. The copy is taken at create, so what the worker gets is what the user confirmed, even if the
source changes later.

**What the user sees.** The receipt lists each file by name and size after the note, so the confirmation covers the
files as well as the text. A held task's files are as invisible to workers as the task.

**What the worker gets.**
- The claim answer adds one line per file: number, name, type and size. It carries no link.
- `get_task_file(task_id, lease_id, file)` (MCP; REST `GET /external-tasks/v1/tasks/{task_id}/files/{n}` with the
  lease id) checks the lease as `renew` does (`lease_refusal`, and a lapsed lease is refused) and answers a signed GET
  URL for that one object (`MediaStoragePort.generate_signed_url`, valid `FILE_LINK_TTL_S`) with the name and size.
- Every call mints a fresh link. A link never has to outlive one download: a worker that needs a file again, an hour
  later or after a restart, asks again, for as long as it holds the lease. The lease is the right to the files; once the
  task is final, the tool answers as for a missing task.
- The worker downloads the file and decides how to read it. The queue never interprets a file.

**Life and cleanup.** The copies are not deleted with the task record (a Firestore TTL cannot delete storage objects,
and a sweeper is a second moving part). The media bucket gets a lifecycle rule for the `external_tasks/` prefix:
delete at `TASK_MAX_AGE_S` plus one day, so every copy outlives every use of its task (a task never outlives its
deadline) and none outlives it by more than a day. The rule is set with the deployment (`docs/07_deployment/README.md`).
Until it is set, the bucket's default age rule (30 days) applies.

**Signing.** The main service's runtime identity signs, as for `/f/` links (signBlob on itself, already granted).

**Not in scope.** Files in a worker's result; text extraction; adding files to an existing task; files after a task is
final.

## 6. Security and threat model

The adversaries:
- a hostile or compromised worker;
- a leaked worker key or OAuth grant;
- outside content (an email, a file, a web page) that Smart read in the same turn and that tries to steer it;
- a user-entered wake-up URL aimed inward.

Coercion of the user and a compromised runtime identity are out of scope, as in `CABINET_DEVICE_SESSIONS_RFC.md` §3.

### 6.1 Prompt injection, both ways

*Superseded in part (2026-11-24): the taint mitigations below are replaced by the owner's confirmation of every chat-created task (decision record `external_task_confirmation_and_alerts.md`). The wrapping of a worker's result stays.*

**Bot → worker.**
- Smart writes the task text in a turn that may also hold outside content. An injected instruction could try to make
  Smart create a task that carries private data out to a worker.
- `interactive_delivery` does not stop this. The user is present, but the user did not ask for that call (§3).
- Mitigations:
  - **Taint (superseded 2026-11-24: every chat create is held).** A create in an execution that saw outside content is held. Only the owner's `$task send <code>` queues
    it and sends the wake-up (§5.3).
  - Every create, held or not, is posted to the owner literally with its full text.
  - Workers are only those the owner registered, and a label must exist.
  - The tool description says that a task leaves the system.
  - The claim result marks the text as written by the assistant, possibly quoting outside material. Whether the
    worker treats it as data is outside this system's control.
- Residual risk: an untainted execution (no outside content entered it) can still produce a task the user did not mean.
  The literal receipt makes it visible, and `cancel` stops it before a claim.

**Worker → bot.**
- A result and a failure reason are hostile by assumption.
- Neither reaches a model at delivery. Delivery runs no agent turn, and its notice and history pair never quote either.
- Every read is wrapped as outside content:
  - `get_external_task`;
  - `workspace` reads of a flagged file, wherever it was renamed or copied;
  - drive reads of the results subfolder.
- Background runs can read a result through `workspace` or the drive tools (§5.9). That read is wrapped too.
- A result cannot forge a system note: the system tag word is neutralised on store, and the wrapper neutralises
  look-alikes.
- **A result cannot enqueue on its own.** Any execution that read a result is tainted, so a create in it is held for the
  owner's `$task send`. A background run has no queue tool at all.
- It cannot write memory: no user-role line carries it, and the four tools set no `history_context`.
- Residual risks:
  - In the turn where the user reads a result, Smart holds its other tools (delegation, `workspace` writes). The
    defence there is the wrapper and the rule, the same as for any email or file.
  - Text Smart retypes from a result into a new file (not `copy` or `rename`) carries no flag.
  - A drive copy the user moves out of the results subfolder loses the path mapping, though its header remains.

### 6.2 Outbound requests

The wake-up is the only outbound call to a user-chosen address. §5.8's guard covers:
- server-side request forgery to internal ranges, the metadata server, `.internal` names and the bot's own hosts;
- addresses that embed IPv4;
- DNS rebinding, redirects and proxies.

It rests on the main service having no VPC egress (§3); a VPC connector reopens it.

The request carries no task data, and the response is discarded unread. The user's notice names only a closed refusal
code. A hostile target therefore learns only that work exists.

### 6.3 Key and grant leakage

- A leaked key gives one worker's view and nothing else:
  - claim and read that worker's queued task texts (never a held one);
  - submit results, which are still untrusted, capped and rate-limited;
  - renew, fail or release tasks, bounded by `MAX_RENEWALS_PER_TASK` and `MAX_CLAIMS_PER_TASK`.
- It reads no memory, no drive, no other worker and no result.
- The user revokes the key in the Cabinet, and the next call is refused. Last-used times show misuse.
- Key guessing is bounded: a shape check comes before any read, and `401`s are rate-limited per address and globally.
- Keys are stored as digests, so a Firestore read yields none. Wake-up secrets are ciphertext.
- An OAuth grant is bound to one worker (`wid`) and to the tasks audience, and is re-checked on every call through
  `grant_allowed`. A grant revoked in the Cabinet stops at its next call. A facts or drive connector never gains the
  scope, because it is not a default scope.

### 6.4 Locked users

Every worker surface checks the lock fresh: MCP tool calls, REST calls, grants, `/worker` task types (which already
check through the hub), `$task send` (through IAM), and channel resolution for notices.
- A locked user's workers get the lock text.
- Tasks stay stored.
- Timers refused during the lock are caught up by the claim's deadline check after an unlock (§5.2).

### 6.5 Tenancy

Tenancy comes from the credential only (§5.7). Not-yours and missing tasks get one refusal. The Smart tools close over
the user, and `$task send` matches a code only among that user's held tasks. A test per surface shows that a task of
user B, or of worker B of the same user, is unreachable with A's credential.

### 6.6 Task files

- A file leaves only with its task: after the user's confirmation (a chat create) or from a reminder the user set,
  exactly like the brief. The receipt names every file.
- The specialist cannot attach what Smart did not name: each reference must appear in its instruction.
- A link is a bearer URL to one object for `FILE_LINK_TTL_S`, minted only for the worker holding the task's lease.
  Logs name the task and the file number, never the link, the name or the key.
- A file is the user's own material; to the worker it is data for the work, as the brief's quoted material is (§6.1).
  Nothing a worker returns is affected.

## 7. Failure modes

| Failure | Behaviour |
|---|---|
| The worker never comes | The deadline timer expires the task. The user gets the code-written expiry notice. |
| The worker claims and dies, repeatedly | Each lease expiry re-queues and re-pings. The claim after `MAX_CLAIMS_PER_TASK` fails the task (`claim_loop`) with a notice. |
| The worker releases a task again and again | Releases count through their claims. The same `claim_loop` cap ends it. |
| Long work outruns the lease | The worker renews (E1) up to `MAX_RENEWALS_PER_TASK`. After that the lease runs out, and a submit still succeeds until another claim takes the task. |
| Lease timer lost | The next claim recovers the expired lease lazily (§5.2). The deadline still bounds the task. |
| Deadline timer refused while the user was locked | The claim expires a candidate past `deadline_at` instead of handing it out (§5.2). |
| An attached file does not resolve at create | The whole create is refused with one tool error naming the file; nothing is stored (§5.12). |
| A file's copy is gone when the worker asks (lifecycle, manual delete) | `get_task_file` answers `file_gone`; the worker goes on without it. |
| Signing a file link fails | A retryable error answer; the worker asks again. |
| A held task is never confirmed | The hold timer expires it silently. No worker ever saw it, and no wake-up left. |
| Wake-up endpoint down (5xx, timeout) | Cloud Tasks retries within its bounds, then gives up. The task stays queued for polling or the next ping. The deadline still applies. |
| Wake-up refused by the guard, or 4xx | Final. One notice to the user naming the worker and a `WakeRefusal` code. The task stays queued. |
| A flood of bad keys | The shape check refuses without a read. Above the `401` limits the endpoint answers `429` before any lookup. |
| Duplicate submit, same body | Same success answer. Nothing changes. |
| Submit with another body after done | `409`. The create-only result document is kept. |
| Submit on a stale lease (re-claimed by another session) | `409`. The other lease decides. |
| Cancel and submit race | One transaction wins. After cancel, submit is refused and nothing is stored. After done, cancel is refused. |
| Result too large, wrong type, invalid UTF-8 | `413` / `415` / `400`. The lease stays, so the worker can retry within it. |
| Delivery copy refused (workspace caps, no primary drive) | The result stays in the result record. The notice says where it can be read. |
| No channel for the notice | `resolve_channel` finds none. Delivery is marked and logged, and the user sees the result through `list_external_tasks`. |
| User locked mid-task | Worker calls are refused with the lock text. Notices are not sent. Expired deadlines are applied at the next claim after unlock. |
| Flag switched off mid-task | Worker calls are refused. Tasks expire. Nothing is deleted. |
| Worker removed mid-task | Its non-final tasks are cancelled. Its keys and grants are refused. |
| Delivery task redelivered | The `/worker` claim plus `delivery.state` make it a no-op. |
| `SECRET_DIGEST_KEY` changes | Every worker key and every pending confirmation code is invalid, as with MCP codes and links. Users mint new keys. |
| Composite index not ready | Claim and listing queries fail at query time. Indexes are deployed and `READY` first. |

## 8. Alternatives rejected

- **The worker registers its own wake-up URL over MCP.** The worker would have to carry the URL's secret in its own
  prompt or configuration and hand it over through a model. The user's Cabinet is the one place where a secret is
  entered, encrypted and never shown again.
- **Pushing task content in the wake-up call.** This would send task text to a URL on every ping and every retry,
  outside the lease. The text would be repeated into logs and proxies along the way, with no claim and no idempotency.
  A content-free ping followed by an authenticated claim keeps content behind the credential.
- **MCP server-initiated push.** An MCP server can reach a client only inside a session the client opened. A worker
  that is not running has no session. Wake-up therefore needs an address the worker owns.
- **Per-use-case integrations** (a "research" feature, a "calendar watch" feature). Each would need new code, tools and
  texts. One queue plus the user's skills covers every case (D1).
- **The result as a user-role session message.** The consolidator would read worker text as the owner's own words
  (D6).
- **Delivering through `notify` with an agent turn.** See §5.9: it puts model-written text into a background turn that
  holds tools.
- **Relying on `interactive_delivery` alone for creates.** The user is present, but outside content read in the same
  turn can still drive the call. Taint plus the held task closes this (§5.3).
- **Taint detected by wrappers alone.** Web search output carries no wrapper (§3). The allowlist of clean sources fails
  safe instead.
- **Confirming every create.** This would be safe, but it adds a step to every hand-off the user plainly asked for.
  Taint confines the step to executions that saw outside content.
- **Marking results by folder only.** `rename` and `copy` would move the text out of the marking. The file-level flag
  follows the file.
- **A label argument on the worker tools for OAuth clients.** One grant could then read every worker's tasks. The grant
  is bound to one worker instead (§5.4).
- **Signed worker keys (JWT, like the voice token).** Revocation needs a server lookup per call anyway (§5.6).
- **Wake-up secrets in the credential store.** That store is OAuth-shaped (§3, E3).
- **Polling only.** Polling is supported, but some workers can only be started from outside. D4 keeps both.

## 9. Steps (vertical slices)

Each step is one or more PRs and is usable on its own.

1. **Queue, Smart, REST, workspace.**
   - Domain: `external_task.py` and its texts.
   - Stores and indexes.
   - `ExternalTaskService`, with every state change as one precondition transaction and the create-only result
     document.
   - The four interactive-only Smart tools: the `TaintTracker`, held tasks, `$task send`, and the literal receipts
     quoting the full text.
   - The worker registry with keys in the Cabinet (Settings → Connections; no wake-up yet).
   - The REST endpoint, renew included, with the shape check and the `401` limits.
   - `external_task_deliver` to the workspace, with the literal notice and history pair. Delivery is a system alert
     since 2026-11-24: the result (or a worker's failure reason) is a workspace file and `notify` runs Smart on a
     code-made alert naming it (`docs/05_building_blocks/external_task_queue/README.md`); no literal notice, no pair.
     The same day's amendment adds the task's note, the bot's own return instructions, to the alert of a task that
     has one: no longer code-made values only (decision record `external_task_confirmation_and_alerts.md`,
     "Amendment").
   - The workspace untrusted flag: set on delivery, carried by every new version, `rename` and `copy`, and wrapped on
     every read.
   - `external_task_timer` (lease, deadline, hold).
   - The flag, written by an operator script.
   - Entry-point rows: `external_task_api`; the `worker` row covers the new task types, and `chat` covers `$task send`.
   - TTL runbook and registry entries.
   - Workers poll.
2. **Wake-up.**
   - `OutboundWebhookPort` and the guard adapter, each rule with its own test: private, metadata and embedded-IPv4
     addresses, `.internal` and single-label names, the bot's own hosts and `run.app`, port, rebinding, redirect, IP
     literal, headers, the closed refusal codes, and the URL never logged.
   - Encrypted targets in the Cabinet.
   - `external_task_wake`, generation and re-ping.
   - SCHEDULERS.md rows.
   - The egress assumption recorded as a deploy trap.
3. **MCP resource on worker keys.**
   - `/tasks-mcp`, `MCPResource.TASKS` and its six tools.
   - Text tokens `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`.
   - Verifier routing.
   - Dispatcher and `MCP_PATHS` / `mcp_task_tools` rows (test edit by reviewer).
4. **OAuth for the tasks resource.**
   - The non-default scope.
   - `bind_resource` gains the third resource.
   - The consent page's worker picker and the `wid` claim.
   - Flag checks at grant and refresh, and `grant_allowed` on every call.
   - The Cabinet's grant list with Revoke.
   - Q1 answered first.
5. **Drive destination** (D13).
   - The per-worker setting.
   - `save_to_primary` into the Inbox's bot-named results subfolder, gated by `can_save_to_primary`.
   - `is_external_results_path`, mapping that subfolder to `external_content` in every drive render: specialist and
     connector, search, passages and open.
6. **Task files** (D15, §5.12).
   - `files` on the record and the specialist's tool, the reference check against the instruction, the copies, the receipt lines.
   - The claim lines, `get_task_file` on MCP and REST, the two text tokens, the limits.
   - The bucket lifecycle rule for `external_tasks/`.
7. **At implementation**: the building block README (`docs/05_building_blocks/external_task_queue/README.md`) with a
   living-doc header, a row in the root `CLAUDE.md` map, and this RFC's status.

## 10. Open questions

1. **Does claude.ai's dynamic registration send a `scope`?** Closed. claude.ai sends its own single scope at dynamic
   registration, one per connector, so a resource that advertises its own scope gets a registration carrying it.
   - Still to check at the OAuth step (step 4): the scope requested at authorize time matches the registered one.

All other questions of the first draft are closed by D11–D13 and E1–E7 (§2).

## 11. Illustrative uses

Each use below is one worker registered in the Cabinet plus one personal skill that tells Smart when to hand off. None
of them needs code.

1. **Long research.** Alex Doe asks the bot to compare three community-garden irrigation kits for Shelbyville. The user's
   `research` skill sends such requests to the worker labelled `research`: an AI worker with web access. It claims the
   task, researches, and submits a text report. The report lands in the workspace, and the bot posts the notice.
2. **Work in a code repository.** A worker attached to one code repository takes tasks labelled `code`. It works on a
   branch, opens a pull request, and submits a short text result with the pull request's link.
   - The protections live at the repository and the worker, not in the bot: pushes only to branches, a protected main
     branch, and human review before any merge.
   - The bot treats the link as text and never fetches it.
3. **A script with no model.** A plain HTTP worker polls `/external-tasks/v1/claim` for tasks labelled `export`. It does
   a deterministic job and submits a text result. It holds a worker key and never touches OAuth.
